import os
import time
import matplotlib.pyplot as plt
from monai.apps import DecathlonDataset
from monai.config import print_config
from monai.data import DataLoader, decollate_batch
from monai.handlers.utils import from_engine
from monai.losses import DiceLoss
from monai.inferers import sliding_window_inference
from monai.metrics import DiceMetric, MeanIoU, ConfusionMatrixMetric
from monai.networks.nets import UNet
from monai.networks.layers import Norm
from tqdm import tqdm
from monai.networks.nets import SegResNet
from monai.utils import set_determinism
from monai.transforms import (
    Activations,
    Activationsd,
    AsDiscrete,
    AsDiscreted,
    Compose,
    Invertd,
    LoadImaged,
    MapTransform,
    NormalizeIntensityd,
    Orientationd,
    RandFlipd,
    RandScaleIntensityd,
    RandShiftIntensityd,
    RandSpatialCropd,
    Spacingd,
    EnsureTyped,
    EnsureChannelFirstd,
)
import torch.nn.functional as F
from sklearn.model_selection import train_test_split
import torch
from torch.utils.data import Dataset
import numpy as np
import wandb
from monai.networks.nets import SwinUNETR
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
import json


standard_shape = (256, 256, 128)
class DatasetMRI(Dataset):
    def __init__(self, data_paths, label_paths, transforms=None, train=True):
        self.data_paths = data_paths
        self.label_paths = label_paths
        self.transforms = transforms
        self.train = train

    def __len__(self):
        return len(self.data_paths)

    def __getitem__(self, idx):
        image = np.load(self.data_paths[idx]).transpose(1, 2, 3, 0)
        mask = np.load(self.label_paths[idx]).transpose(1, 2, 3, 0)

        image = torch.tensor(image.astype(np.float32)).unsqueeze(0)
        image = F.interpolate(image, size=(standard_shape), mode='trilinear', align_corners=False).squeeze(0)
        if self.train:
            mask = torch.tensor(mask.astype(np.float32)).unsqueeze(0)
            mask = F.interpolate(mask, size=(standard_shape), mode='trilinear', align_corners=False).squeeze(0)
        sample = {'image': image, 'label': mask}
        if self.transforms:
            sample = self.transforms(sample)
        if self.train:
            label = sample['label'].as_tensor()
        else:
            label = torch.tensor(sample['label'].astype(np.float32))
            image = sample['image']

        return image, label

def calculate_metrics(actual, predictions):
    accuracy = accuracy_score(actual, predictions)
    precision = precision_score(actual, predictions)
    recall = recall_score(actual, predictions)
    f1 = f1_score(actual, predictions)
    return accuracy, precision, recall, f1


def main():
    save_dir = 'weights/'
    set_determinism(seed=0)
        
    test_transform = Compose(
        [
            NormalizeIntensityd(keys="image", nonzero=True, channel_wise=True),
        ]
    )

    data_dirs = ['data/RIDER/', 'data/DUKE/']
    individual_test_image_paths, individual_test_mask_paths = [], []
    for data_dir in data_dirs:
        images_path = os.path.join(data_dir, 'images')
        images_st_path = os.path.join(data_dir, 'images_std')
        masks_path = os.path.join(data_dir, 'masks')
        image_paths = os.listdir(images_path)
        image_st_paths = [os.path.join(images_st_path, image_path) for image_path in image_paths]
        mask_paths = [os.path.join(masks_path, image_path) for image_path in image_paths]
        image_paths = [os.path.join(images_path, image_path) for image_path in image_paths]
        individual_test_image_paths.append(image_st_paths)
        individual_test_mask_paths.append(mask_paths)

    test_ds0 = DatasetMRI(individual_test_image_paths[0], individual_test_mask_paths[0], test_transform, train=False)
    test_ds1 = DatasetMRI(individual_test_image_paths[1], individual_test_mask_paths[1], test_transform, train=False)
    test_loader0 = DataLoader(test_ds0, batch_size=1, shuffle=False)
    test_loader1 = DataLoader(test_ds1, batch_size=1, shuffle=False) 
    individual_test_loaders = [test_loader0, test_loader1]
    individual_names = ['RIDER', 'DUKE']

    device = torch.device("cuda:3")
    model = UNet(
        spatial_dims=2,
        in_channels=9,
        out_channels=3,
        channels=(16, 32, 64, 128, 256),
        strides=(2, 2, 2, 2),
        num_res_units=2,
        norm=Norm.BATCH,
    ).to(device)
    checkpoint_path = 'weights/UNet2-1D.pth'
    checkpoint = torch.load(checkpoint_path)
    model.load_state_dict(checkpoint)

    torch.backends.cudnn.benchmark = True
    dice_metric = DiceMetric(include_background=True, reduction="mean")
    dice_metric_batch = DiceMetric(include_background=True, reduction="mean_batch")
    iou_metric = MeanIoU(include_background=True, reduction="mean")
    iou_metric_batch = MeanIoU(include_background=True, reduction="mean_batch")
    tpf_metric = ConfusionMatrixMetric(metric_name="sensitivity", reduction="mean")
    tpf_metric_batch = ConfusionMatrixMetric(metric_name="sensitivity", reduction="mean_batch")
    post_trans = Compose([Activations(sigmoid=True), AsDiscrete(threshold=0.5)])

    # EVAL
    model.eval()
    batch_size = 32
    with torch.no_grad():
        for i, individual_test_loader in tqdm(enumerate(individual_test_loaders)):
            dataset = individual_names[i]
            accuracies, precisions, recalls, f1s = [], [], [], []
            for image, label in individual_test_loader:
                if dataset == 'RIDER':
                    image = image[:, 0].unsqueeze(0)  # only uses first channel
                image = image.repeat(1, 3, 1, 1, 1)
                image = image.as_tensor().squeeze().permute(-1, 0, 1, 2)
                outputs = []
                for i in range(0, image.shape[0], batch_size):
                    images = image[i:i+batch_size]
                    if i == 0:
                        before_image = image[0]
                    else:
                        before_image = image[i-1]
                    before_images = images.clone()[:-1]
                    before_images = torch.concatenate([before_image.unsqueeze(0), before_images])
                    if i + batch_size >= image.shape[0]:
                        after_image = image[-1]
                    else:
                        after_image = image[i+batch_size+1]
                    after_images = images.clone()[1:]
                    after_images = torch.concatenate([after_images, after_image.unsqueeze(0)])
                    images = torch.concatenate([before_images, images, after_images], dim=1)
                    outputs.append(model(images.to(device)))
                output = torch.concatenate(outputs)
                output = output.permute(1, 2, 3, 0).unsqueeze(0)
                output = F.interpolate(output, size=(list(label.shape)[2:]), mode='trilinear', align_corners=False).cpu()
                output = post_trans(output)[:, 0].unsqueeze(0)

                if dataset == 'RIDER':
                    dice_metric(y_pred=output, y=label)
                    iou_metric(y_pred=output, y=label)
                    tpf_metric(y_pred=output, y=label)

                if dataset == 'DUKE':
                    y, yhat = np.zeros(label.shape[-1]), np.zeros(label.shape[-1])
                    nonzero_indices = torch.nonzero(output > 0, as_tuple=False)
                    positive_images = torch.unique(nonzero_indices[:, -1])
                    yhat[positive_images] = 1
                    nonzero_indices = torch.nonzero(label > 0, as_tuple=False)
                    positive_images = torch.unique(nonzero_indices[:, -1])
                    y[positive_images] = 1
                    accuracy, precision, recall, f1 = calculate_metrics(y, yhat)
                    accuracies.append(accuracy)
                    precisions.append(precision)
                    recalls.append(recall)
                    f1s.append(f1)

            if dataset == 'RIDER':
                metric = dice_metric.aggregate().item()
                metric2 = iou_metric.aggregate().item()
                metric3 = tpf_metric.aggregate()[0].item()
                print(
                    f"{dataset}"
                    f"\nmean dice: {metric:.4f}"
                    f"\nmean iou: {metric2:.4f}"
                    f"\nmean tpf: {metric3:.4f}"
                )
                dice_metric.reset()
                dice_metric_batch.reset()
                iou_metric.reset()
                iou_metric_batch.reset()
                tpf_metric.reset()
                tpf_metric_batch.reset()
            elif dataset == 'DUKE':
                print(f'mean accuracy: {np.mean(accuracies):.4f}'
                      f'\nmean precision: {np.mean(precisions):.4f}'
                      f'\nmean recall: {np.mean(recalls):.4f}'
                      f'\nmean f1: {np.mean(f1s):.4f}')

if __name__ == "__main__":
    main()