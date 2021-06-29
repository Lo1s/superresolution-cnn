import numpy as np
import torch
import torch.nn as nn
from pathlib import Path

from torchvision import models
from torchvision.utils import save_image, make_grid
from tqdm import tqdm
from base import BaseTrainer
from model.esrgan.utils import SINGLE_KEY
from model.srcnn.metric import psnr
from model.unet.loss import create_loss_model
from utils import inf_loop, MetricTracker


class SRCNNTrainer(BaseTrainer):
    """
    Trainer class
    """

    def __init__(self, model, criterion, metric_ftns, optimizer, config, device, data_loader,
                 valid_data_loader=None, lr_scheduler=None, len_epoch=None, logging=True, use_vgg_loss=False,
                 monitor_cfg_key='monitor'):
        super().__init__([model], criterion, metric_ftns, [optimizer], config, device, monitor_cfg_key=monitor_cfg_key)
        self.config = config
        self.data_loader = data_loader
        if len_epoch is None:
            # epoch-based training
            self.len_epoch = len(self.data_loader)
        else:
            # iteration-based training
            self.data_loader = inf_loop(data_loader)
            self.len_epoch = len_epoch
        self.valid_data_loader = valid_data_loader
        self.do_validation = self.valid_data_loader is not None
        self.lr_scheduler = lr_scheduler
        self.log_step = int(np.sqrt(data_loader.batch_size))
        self.logging = logging

        # vgg loss
        vgg16 = models.vgg16(pretrained=True).features
        if torch.cuda.is_available():
            vgg16.cuda(device=device)
        self.vgg_loss = create_loss_model(vgg16, 8, use_cuda=torch.cuda.is_available(), device=device)
        self.use_vgg_loss = use_vgg_loss

        self.train_metrics = MetricTracker('loss', *[m.__name__ for m in self.metric_ftns], writer=self.writer)
        self.valid_metrics = MetricTracker('loss', *[m.__name__ for m in self.metric_ftns], writer=self.writer)

    def _train_epoch(self, epoch):
        """
        Training logic for an epoch
        :param epoch: Integer, current training epoch.
        :return: A log that contains average loss and metric in this epoch.
        """
        self.models[SINGLE_KEY].train()
        self.train_metrics.reset()
        for batch_idx, (data, target) in enumerate(tqdm(self.data_loader)):
            data, target = data.to(self.device), target.to(self.device)

            self.optimizers[SINGLE_KEY].zero_grad()
            output = self.models[SINGLE_KEY](data)

            if self.use_vgg_loss:
                output_vgg_loss = self.vgg_loss(output)
                target_vgg_loss = self.vgg_loss(target)
                loss = self.criterion(output_vgg_loss, target_vgg_loss)
            else:
                loss = self.criterion(output, target)

            loss.backward()
            self.optimizers[SINGLE_KEY].step()

            self.writer.set_step((epoch - 1) * self.len_epoch + batch_idx)
            self.train_metrics.update('loss', loss.item())
            for met in self.metric_ftns:
                self.train_metrics.update(met.__name__, met(output, target))

            if batch_idx % self.log_step == 0 and self.logging:
                self.logger.debug('\nTrain Epoch: {} {} Loss: {:.6f}'.format(
                    epoch,
                    self._progress(batch_idx),
                    loss.item()))
                self.writer.add_image('input', make_grid(data.cpu(), nrow=8, normalize=True))

            if batch_idx == self.len_epoch:
                break
        log = self.train_metrics.result()

        if self.do_validation:
            val_log = self._valid_epoch(epoch)
            log.update(**{'val_'+k : v for k, v in val_log.items()})

        if self.lr_scheduler is not None:
            self.lr_scheduler.step()
        return log

    def _valid_epoch(self, epoch):
        """
        Validate after training an epoch
        :param epoch: Integer, current training epoch.
        :return: A log that contains information about validation
        """
        self.models[SINGLE_KEY].eval()
        self.valid_metrics.reset()
        with torch.no_grad():
            for batch_idx, (data, target) in enumerate(self.valid_data_loader):
                data, target = data.to(self.device), target.to(self.device)
                output = self.models[SINGLE_KEY](data)

                if self.use_vgg_loss:
                    output_vgg_loss = self.vgg_loss(output)
                    target_vgg_loss = self.vgg_loss(target)
                    loss = self.criterion(output_vgg_loss, target_vgg_loss)
                else:
                    loss = self.criterion(output, target)

                self.writer.set_step((epoch - 1) * len(self.valid_data_loader) + batch_idx, 'valid')
                self.valid_metrics.update('loss', loss.item())
                for met in self.metric_ftns:
                    self.valid_metrics.update(met.__name__, met(output, target))
                self.writer.add_image('input', make_grid(data.cpu(), nrow=8, normalize=True))

        # add histogram of the model parameters to the tensorboard
        for name, p in self.models[SINGLE_KEY].named_parameters():
            self.writer.add_histogram(name, p, bins='auto')
        return self.valid_metrics.result()

    def _progress(self, batch_idx):
        base = '[{}/{} ({:.0f}%)]'
        if hasattr(self.data_loader, 'n_samples'):
            current = batch_idx * self.data_loader.batch_size
            total = self.data_loader.n_samples
        else:
            current = batch_idx
            total = self.len_epoch
        return base.format(current, total, 100.0 * current / total)
