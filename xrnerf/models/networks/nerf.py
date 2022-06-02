# Copyright (c) OpenMMLab. All rights reserved.
import torch
from torch import nn
from tqdm import tqdm
from mmcv.runner import get_dist_info

from .. import builder
from ..builder import NETWORKS
from .base import BaseNerfNetwork
from .utils import *


@NETWORKS.register_module()
class NerfNetwork(BaseNerfNetwork):
    '''
        There are 3 kinds of forward mode for Network:
            1. 'train': phase=='train' and use 'train_step()' to forward, input a batch of rays
            2. 'val': phase=='train' and 'val_step()' to forward, input all testset's poses&images in one 'val_step()'
            3. 'test': phase=='test' and 'test_step()' to forward, input all testset one by one
    '''

    def __init__(self, cfg, mlp=None, mlp_fine=None, render=None):
        super().__init__()

        self.phase = cfg.get('phase', 'train')
        if 'chunk' in cfg: self.chunk = cfg.chunk
        if 'is_perturb' in cfg: self.is_perturb = cfg.is_perturb
        if 'N_importance' in cfg: self.N_importance = cfg.N_importance
        
        if mlp is not None:
            self.mlp = builder.build_mlp(mlp)
        if mlp_fine is not None:            
            self.mlp_fine = builder.build_mlp(mlp_fine)
        if render is not None:
            self.render = builder.build_render(render)

    def forward(self, data, is_test=False):
        
        data, ret = self.render(self.mlp(data), is_test)
        if self.N_importance>0:
            data = sample_pdf(data, self.N_importance, self.is_perturb, is_test)
            _, fine_ret = self.render(self.mlp_fine(data), is_test)
            ret = merge_ret(ret, fine_ret) # add fine-net's returns to ret

        return ret

    def batchify_forward(self, data, is_test=False):
        """forward in smaller minibatches to avoid OOM.
            除了mlp的chunk这里也有chunk，因为emb操作可能超显存
            考虑只保留此处的chunk
        """
        N = data['rays_o'].shape[0]
        all_ret = {}
        for i in range(0, N, self.chunk):
            # 准备数据
            data_chunk = {}
            for k in data:
                if data[k].shape[0]==N:
                    data_chunk[k] = data[k][i:i+self.chunk]
                else:
                    data_chunk[k] = data[k]
            # run
            ret = self.forward(data_chunk, is_test)
            # 拼接结果
            for k in ret:
                if k not in all_ret: all_ret[k] = []
                all_ret[k].append(ret[k])
        all_ret = {k : torch.cat(all_ret[k], 0) for k in all_ret}
        return all_ret

    def train_step(self, data, optimizer, **kwargs):
        for k in data:
            data[k] = unfold_batching(data[k])
        ret = self.forward(data, is_test=False)

        img_loss = img2mse(ret['rgb'], data['target_s'])
        psnr = mse2psnr(img_loss)
        loss = img_loss

        if 'rgb0' in ret:
            img_loss0 = img2mse(ret['rgb0'], data['target_s'])
            loss = loss + img_loss0
            psnr0 = mse2psnr(img_loss0)

        log_vars = {'loss':loss.item(), 'psnr':psnr.item()}
        outputs = {'loss':loss, 'log_vars':log_vars, 'num_samples':ret['rgb'].shape[0]}
        return outputs

    def val_step(self, data, optimizer=None, **kwargs):
        if self.phase=='test':
            return self.test_step(data, **kwargs)

        rank, world_size = get_dist_info()
        if rank==0:        
            for k in data:
                data[k] = unfold_batching(data[k])        
            poses = data['poses']
            images = data['images']
            spiral_poses = data['spiral_poses']

            rgbs, disps, gt_imgs = [], [], []
            for i in tqdm(range(poses.shape[0])):
                data = self.val_pipeline({'pose':poses[i]})
                ret = self.batchify_forward(data, is_test=True) # 测试时 raw_noise_std=False
                rgb = recover_shape(ret['rgb'], data['src_shape'])
                disp = recover_shape(ret['disp'], data['src_shape'])
                rgbs.append(rgb.cpu().numpy())
                disps.append(disp.cpu().numpy())
                gt_imgs.append(images[i].cpu().numpy())

            spiral_rgbs, spiral_disps = [], []
            for i in tqdm(range(spiral_poses.shape[0])):
                data = self.val_pipeline({'pose':spiral_poses[i]})
                ret = self.batchify_forward(data, is_test=True)
                rgb = recover_shape(ret['rgb'], data['src_shape'])
                disp = recover_shape(ret['disp'], data['src_shape'])
                spiral_rgbs.append(rgb.cpu().numpy())
                spiral_disps.append(disp.cpu().numpy())

            outputs = {'spiral_rgbs':spiral_rgbs, 'spiral_disps':spiral_disps, 
                        'rgbs':rgbs, 'disps':disps, 'gt_imgs':gt_imgs}                
        else:
            outputs = {}
        return outputs

    def test_step(self, data, **kwargs):
        '''
            in mmcv's runner, there is only train_step and val_step
            so use [val_step() + phase=='test'] to represent test
        '''
        rank, world_size = get_dist_info()
        if rank==0:        
            for k in data:
                data[k] = unfold_batching(data[k])

            image = data['image']
            data = self.val_pipeline({'pose':data['pose']})

            ret = self.batchify_forward(data, is_test=True)
            rgb = recover_shape(ret['rgb'], data['src_shape'])

            rgb = rgb.cpu().numpy()
            image = image.cpu().numpy()
            
            outputs = {'rgb':rgb, 'gt_img':image}

        else:
            outputs = {}
        return outputs

    def set_val_pipeline(self, func):
        self.val_pipeline = func
        return
