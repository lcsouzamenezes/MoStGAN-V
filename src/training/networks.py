﻿# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

from unittest.mock import NonCallableMagicMock
import numpy as np
import torch
from torch import Tensor, instance_norm
import torch.nn as nn
import torch.nn.functional as F
from omegaconf import OmegaConf
import math

from src.torch_utils import misc
from src.torch_utils import persistence
from src.torch_utils.ops import conv2d_resample, upfirdn2d, bias_act, fma

from training.motion import MotionMappingNetwork
from training.layers import (
    FullyConnectedLayer,
    GenInput,
    TemporalDifferenceEncoder,
    Conv2dLayer,
    MappingNetwork,
    EqualLinear,
    tensor_modulation_word,
)


# ----------------------------------------------------------------------------

def modulated_conv2d(
        x,  # Input tensor of shape [batch_size, in_channels, in_height, in_width].
        weight,  # Weight tensor of shape [out_channels, in_channels, kernel_height, kernel_width].
        styles,  # Modulation coefficients of shape [batch_size, in_channels].
        noise=None,  # Optional noise tensor to add to the output activations.
        up=1,  # Integer upsampling factor.
        down=1,  # Integer downsampling factor.
        padding=0,  # Padding with respect to the upsampled image.
        resample_filter=None,
        # Low-pass filter to apply when resampling activations. Must be prepared beforehand by calling upfirdn2d.setup_filter().
        demodulate=True,  # Apply weight demodulation?
        flip_weight=True,  # False = convolution, True = correlation (matches torch.nn.functional.conv2d).
        fused_modconv=True,  # Perform modulation, convolution, and demodulation as a single fused operation?
):
    batch_size = x.shape[0]
    out_channels, in_channels, kh, kw = weight.shape
    misc.assert_shape(weight, [out_channels, in_channels, kh, kw])  # [OIkk]
    misc.assert_shape(x, [batch_size, in_channels, None, None])  # [NIHW]
    # misc.assert_shape(styles, [batch_size, in_channels]) # [NI]
    styles, style_m = styles

    # Pre-normalize inputs to avoid FP16 overflow.
    if x.dtype == torch.float16 and demodulate:
        weight = weight * (1 / np.sqrt(in_channels * kh * kw) / weight.norm(float('inf'), dim=[1, 2, 3],
                                                                            keepdim=True))  # max_Ikk
        if styles is not None:
            styles = styles / styles.norm(float('inf'), dim=1, keepdim=True)  # max_I
    sim = torch.zeros(1).to(x.device)
    # Calculate per-sample weights and demodulation coefficients.
    w = None
    dcoefs = None
    if demodulate or fused_modconv:
        if style_m is not None:
            if styles is not None:
                w = weight.unsqueeze(0) * styles.reshape(batch_size, 1, -1, 1, 1)  # [NOIkk]
            w, sim = tensor_modulation_word(w, style_m,
                                       normalize=(x.dtype == torch.float16 and demodulate))  # [NOIkk]
        else:
            w = weight.unsqueeze(0) * styles.reshape(batch_size, 1, -1, 1, 1)  # [NOIkk]

    if demodulate:
        dcoefs = (w.square().sum(dim=[2, 3, 4]) + 1e-8).rsqrt()  # [NO]
    if demodulate and fused_modconv:
        w = w * dcoefs.reshape(batch_size, -1, 1, 1, 1)  # [NOIkk]

    # Execute by scaling the activations before and after the convolution.
    if not fused_modconv:
        if styles is not None:
            x = x * styles.to(x.dtype).reshape(batch_size, -1, 1, 1)
        x = conv2d_resample.conv2d_resample(x=x, w=weight.to(x.dtype), f=resample_filter, up=up, down=down,
                                            padding=padding, flip_weight=flip_weight)
        if demodulate and noise is not None:
            x = fma.fma(x, dcoefs.to(x.dtype).reshape(batch_size, -1, 1, 1), noise.to(x.dtype))
        elif demodulate:
            x = x * dcoefs.to(x.dtype).reshape(batch_size, -1, 1, 1)
        elif noise is not None:
            x = x.add_(noise.to(x.dtype))
        return x, sim

    # Execute as one fused op using grouped convolution.
    with misc.suppress_tracer_warnings():  # this value will be treated as a constant
        batch_size = int(batch_size)
    misc.assert_shape(x, [batch_size, in_channels, None, None])
    x = x.reshape(1, -1, *x.shape[2:])
    w = w.reshape(-1, in_channels, kh, kw)
    x = conv2d_resample.conv2d_resample(x=x, w=w.to(x.dtype), f=resample_filter, up=up, down=down, padding=padding,
                                        groups=batch_size, flip_weight=flip_weight)
    x = x.reshape(batch_size, -1, *x.shape[2:])
    if noise is not None:
        x = x.add_(noise)
    return x, sim


# ----------------------------------------------------------------------------

@persistence.persistent_class
class SynthesisLayer(torch.nn.Module):
    def __init__(self,
                 in_channels,  # Number of input channels.
                 out_channels,  # Number of output channels.
                 w_dim,  # Intermediate latent (W) dimensionality.
                 resolution,  # Resolution of this layer.
                 kernel_size=3,  # Convolution kernel size.
                 up=1,  # Integer upsampling factor.
                 activation='lrelu',  # Activation function: 'relu', 'lrelu', etc.
                 resample_filter=[1, 3, 3, 1],  # Low-pass filter to apply when resampling activations.
                 conv_clamp=None,  # Clamp the output of convolution layers to +-X, None = disable clamping.
                 channels_last=False,  # Use channels_last format for the weights?
                 word_mod=True,
                 instance_norm=False,
                 cfg={},  # Additional config
                 ):
        super().__init__()

        self.cfg = cfg
        self.resolution = resolution
        self.up = up
        self.activation = activation
        self.conv_clamp = conv_clamp
        self.register_buffer('resample_filter', upfirdn2d.setup_filter(resample_filter))
        self.padding = kernel_size // 2
        self.act_gain = bias_act.activation_funcs[activation].def_gain
        self.word_mod = word_mod
        self.instance_norm = instance_norm

        if self.cfg.style_z or not self.word_mod:
            self.affine = FullyConnectedLayer(w_dim, in_channels, bias_init=1)
        memory_format = torch.channels_last if channels_last else torch.contiguous_format

        self.weight = torch.nn.Parameter(
            torch.randn([out_channels, in_channels, kernel_size, kernel_size]).to(memory_format=memory_format)
        )

        if self.cfg.use_noise:
            self.register_buffer('noise_const', torch.randn([resolution, resolution]))
            self.noise_strength = torch.nn.Parameter(torch.zeros([]))
        self.bias = torch.nn.Parameter(torch.zeros([out_channels]))

        if self.cfg.word_mod and self.word_mod:
            self.hypernet = FullyConnectedLayer(self.cfg.motion_dim, self.cfg.rank*(in_channels + kernel_size + kernel_size), bias_init=1)

    def forward(self, x, w, c, noise_mode='random', fused_modconv=True, gain=1):
        assert noise_mode in ['random', 'const', 'none']
        in_resolution = self.resolution // self.up
        misc.assert_shape(x, [None, self.weight.shape[1], in_resolution, in_resolution])

        style_z = self.affine(w) if self.cfg.style_z or not self.word_mod else None

        if self.cfg.word_mod and self.word_mod:
            n_words = c.shape[1]
            style_m = self.hypernet(c.reshape(-1, self.cfg.motion_dim))  # b,num,in+kh+kw
            style_m = style_m.reshape(-1, n_words, self.cfg.rank, style_m.shape[-1]//self.cfg.rank)
        else:
            style_m = None

        styles = [style_z, style_m]

        noise = None
        if self.cfg.use_noise and noise_mode == 'random':
            noise = torch.randn([x.shape[0], 1, self.resolution, self.resolution],
                                device=x.device) * self.noise_strength
        if self.cfg.use_noise and noise_mode == 'const':
            noise = self.noise_const * self.noise_strength

        if self.instance_norm:
            x = (x - x.mean(dim=(2, 3), keepdim=True)) / (
                    x.std(dim=(2, 3), keepdim=True) + 1e-8)  # [batch_size, c, h, w]

        flip_weight = (self.up == 1)  # slightly faster
        x, sim = modulated_conv2d(x=x, weight=self.weight, styles=styles, noise=noise, up=self.up,
                             padding=self.padding, resample_filter=self.resample_filter, flip_weight=flip_weight,
                             fused_modconv=fused_modconv)

        act_gain = self.act_gain * gain
        act_clamp = self.conv_clamp * gain if self.conv_clamp is not None else None
        x = bias_act.bias_act(x, self.bias.to(x.dtype), act=self.activation, gain=act_gain, clamp=act_clamp)
        return x, sim


# ----------------------------------------------------------------------------

@persistence.persistent_class
class ToRGBLayer(torch.nn.Module):
    def __init__(self, in_channels, out_channels, w_dim, kernel_size=1, conv_clamp=None, channels_last=False):
        super().__init__()
        self.conv_clamp = conv_clamp
        self.affine = FullyConnectedLayer(w_dim, in_channels, bias_init=1)
        memory_format = torch.channels_last if channels_last else torch.contiguous_format
        self.weight = torch.nn.Parameter(
            torch.randn([out_channels, in_channels, kernel_size, kernel_size]).to(memory_format=memory_format)
        )
        self.bias = torch.nn.Parameter(torch.zeros([out_channels]))
        self.weight_gain = 1 / np.sqrt(in_channels * (kernel_size ** 2))

    def forward(self, x, w, fused_modconv=True):
        styles = self.affine(w) * self.weight_gain
        styles = [styles, None]
        x, sims = modulated_conv2d(x=x, weight=self.weight, styles=styles, demodulate=False, fused_modconv=fused_modconv)
        x = bias_act.bias_act(x, self.bias.to(x.dtype), clamp=self.conv_clamp)
        return x


# ----------------------------------------------------------------------------

@persistence.persistent_class
class SynthesisBlock(torch.nn.Module):
    def __init__(self,
                 in_channels,  # Number of input channels, 0 = first block.
                 out_channels,  # Number of output channels.
                 w_dim,  # Intermediate latent (W) dimensionality.
                 motion_v_dim,  # Motion code size
                 resolution,  # Resolution of this block.
                 img_channels,  # Number of output color channels.
                 is_last,  # Is this the last block?
                 architecture='skip',  # Architecture: 'orig', 'skip', 'resnet'.
                 resample_filter=[1, 3, 3, 1],  # Low-pass filter to apply when resampling activations.
                 conv_clamp=None,  # Clamp the output of convolution layers to +-X, None = disable clamping.
                 use_fp16=False,  # Use FP16 for this block?
                 word_mod=True,
                 use_attention=False,
                 fp16_channels_last=False,  # Use channels-last memory format with FP16?
                 cfg={},  # Additional config
                 **layer_kwargs,  # Arguments for SynthesisLayer.
                 ):
        assert architecture in ['orig', 'skip', 'resnet']
        super().__init__()

        self.cfg = cfg
        self.in_channels = in_channels
        self.w_dim = w_dim
        self.resolution = resolution
        self.img_channels = img_channels
        self.is_last = is_last
        self.architecture = architecture
        self.use_fp16 = use_fp16
        self.channels_last = (use_fp16 and fp16_channels_last)
        self.register_buffer('resample_filter', upfirdn2d.setup_filter(resample_filter))
        self.num_conv = 0
        self.num_torgb = 0
        self.use_attention = use_attention

        if in_channels == 0:
            self.input = GenInput(self.cfg, out_channels, motion_v_dim=motion_v_dim)
            conv1_in_channels = self.input.total_dim
        else:
            self.conv0 = SynthesisLayer(in_channels, out_channels, w_dim=w_dim, resolution=self.resolution, up=2,
                                        resample_filter=resample_filter, conv_clamp=conv_clamp,
                                        channels_last=self.channels_last,
                                        kernel_size=3, word_mod=word_mod, cfg=cfg, **layer_kwargs)
            self.num_conv += 1
            conv1_in_channels = out_channels

        self.conv1 = SynthesisLayer(conv1_in_channels, out_channels, w_dim=w_dim, resolution=self.resolution,
                                    conv_clamp=conv_clamp, word_mod=word_mod, channels_last=self.channels_last,
                                    kernel_size=3, instance_norm=False, cfg=cfg,
                                    **layer_kwargs)
        self.num_conv += 1

        if self.use_attention:
            self.tsa = TSA(out_channels)

        if is_last or architecture == 'skip':
            self.torgb = ToRGBLayer(out_channels, img_channels, w_dim=w_dim,
                                    conv_clamp=conv_clamp, channels_last=self.channels_last)
            self.num_torgb += 1

        if in_channels != 0 and architecture == 'resnet':
            self.skip = Conv2dLayer(in_channels, out_channels, kernel_size=1, bias=False, up=2,
                                    resample_filter=resample_filter, channels_last=self.channels_last)

    def forward(self, x, img, ws, c=None, ms=None, motion_v=None, force_fp32=False, fused_modconv=None, **layer_kwargs):
        misc.assert_shape(ws, [None, self.num_conv + self.num_torgb, self.w_dim])
        w_iter = iter(ws.unbind(dim=1))
        m_iter = iter(ms.unbind(dim=1))
        dtype = torch.float16 if self.use_fp16 and not force_fp32 else torch.float32
        memory_format = torch.channels_last if self.channels_last and not force_fp32 else torch.contiguous_format

        if fused_modconv is None:
            with misc.suppress_tracer_warnings():  # this value will be treated as a constant
                fused_modconv = (not self.training) and (
                        dtype == torch.float32 or (isinstance(x, Tensor) and int(x.shape[0]) == 1))

        # Input.
        if self.in_channels == 0:
            x = self.input(ws.shape[0], motion_v=motion_v, dtype=dtype, memory_format=memory_format)
        else:
            misc.assert_shape(x, [None, self.in_channels, self.resolution // 2, self.resolution // 2])
            x = x.to(dtype=dtype, memory_format=memory_format)

        # Main layers.
        sims = 0.0
        if self.in_channels == 0:
            x, sim = self.conv1(x, next(w_iter), next(m_iter), fused_modconv=fused_modconv, **layer_kwargs)
            sims += sim
        elif self.architecture == 'resnet':
            y = self.skip(x, gain=np.sqrt(0.5))
            x, sim = self.conv0(x, next(w_iter), next(m_iter), fused_modconv=fused_modconv, **layer_kwargs)
            sims += sim
            x, sim = self.conv1(x, next(w_iter), next(m_iter), fused_modconv=fused_modconv, gain=np.sqrt(0.5),
                           **layer_kwargs)
            sims += sim
            x = y.add_(x)
        else:
            x, sim = self.conv0(x, next(w_iter), next(m_iter), fused_modconv=fused_modconv, **layer_kwargs)
            sims += sim
            x, sim = self.conv1(x, next(w_iter), next(m_iter), fused_modconv=fused_modconv, **layer_kwargs)
            sims += sim

        # Attention module
        if self.use_attention:
            ori_type = x.dtype
            x = self.tsa(x.to(torch.float32)).to(ori_type)

        # ToRGB.
        if img is not None:
            misc.assert_shape(img, [None, self.img_channels, self.resolution // 2, self.resolution // 2])
            img = upfirdn2d.upsample2d(img, self.resample_filter)

        if self.is_last or self.architecture == 'skip':
            y = self.torgb(x, next(w_iter), fused_modconv=fused_modconv)
            y = y.to(dtype=torch.float32, memory_format=torch.contiguous_format)
            img = img.add_(y) if img is not None else y

        assert x.dtype == dtype
        assert img is None or img.dtype == torch.float32
        return x, img, sims


# ----------------------------------------------------------------------------

@persistence.persistent_class
class SynthesisNetwork(torch.nn.Module):
    def __init__(self,
                 w_dim,  # Intermediate latent (W) dimensionality.
                 img_resolution,  # Output image resolution.
                 img_channels,  # Number of color channels.
                 channel_base=32768,  # Overall multiplier for the number of channels.
                 channel_max=512,  # Maximum number of channels in any layer.
                 num_fp16_res=0,  # Use FP16 for the N highest resolutions.
                 cfg={},  # Additional config
                 **block_kwargs,  # Arguments for SynthesisBlock.
                 ):
        assert img_resolution >= 4 and img_resolution & (img_resolution - 1) == 0
        super().__init__()

        self.w_dim = w_dim
        self.cfg = cfg
        self.img_resolution = img_resolution
        self.img_resolution_log2 = int(np.log2(img_resolution))
        self.img_channels = img_channels
        self.block_resolutions = [2 ** i for i in range(2, self.img_resolution_log2 + 1)]
        channels_dict = {res: min(channel_base // res, channel_max) for res in self.block_resolutions}
        fp16_resolution = max(2 ** (self.img_resolution_log2 + 1 - num_fp16_res), 8)

        if self.cfg.motion.v_dim > 0:
            self.motion_encoder = MotionMappingNetwork(self.cfg)
            self.motion_v_dim = self.motion_encoder.get_dim()
        else:
            self.motion_encoder = None
            self.motion_v_dim = 0

        self.motion_num = self.cfg.motion_num

        if self.motion_num > 0:
            if self.cfg.consistent_motion:
                self.multimotionmap = torch.nn.Sequential(
                    FullyConnectedLayer(self.w_dim, self.motion_num * self.w_dim, activation='lrelu'),
                    FullyConnectedLayer(self.motion_num * self.w_dim, self.motion_num * self.cfg.motion_dim),
                )
            else:
                self.multimotionmap = torch.nn.Sequential(
                    FullyConnectedLayer(self.motion_v_dim + self.w_dim, self.motion_num * self.w_dim, activation='lrelu'),
                    FullyConnectedLayer(self.motion_num * self.w_dim, self.motion_num * self.cfg.motion_dim),
                )

        self.num_ws = 0
        for res in self.block_resolutions:
            in_channels = channels_dict[res // 2] if res > 4 else 0
            out_channels = channels_dict[res]
            use_fp16 = (res >= fp16_resolution)
            is_last = (res == self.img_resolution)
            # which block use word modulation
            word_mod = True if str(res) in self.cfg.mod_layers.split(',') else False
            use_attention = True if str(res) in self.cfg.use_attention.split(',') else False
            block = SynthesisBlock(
                in_channels,
                out_channels,
                w_dim=self.w_dim + (self.motion_v_dim if self.cfg.time_enc.cond_type == 'concat_w' else 0),
                motion_v_dim=self.motion_v_dim,
                resolution=res,
                img_channels=img_channels,
                is_last=is_last,
                use_fp16=use_fp16,
                word_mod=word_mod,
                use_attention=use_attention,
                cfg=cfg,
                **block_kwargs)
            self.num_ws += block.num_conv

            if is_last:
                self.num_ws += block.num_torgb
            setattr(self, f'b{res}', block)

    def forward(self, ws, t=None, c=None, motion_z=None, motion_v=None, **block_kwargs):
        assert len(ws) == len(c) == len(t), f"Wrong shape: {ws.shape}, {c.shape}, {t.shape}"
        assert t.ndim == 2, f"Wrong shape: {t.shape}"

        misc.assert_shape(ws, [None, self.num_ws, self.w_dim])
        block_ws = []
        block_ms = []

        if self.motion_encoder is None:
            motion_v = None
        else:
            if motion_v is None:
                motion_info = self.motion_encoder(c, t, motion_z=motion_z)  # [batch_size * num_frames, motion_v_dim]
                motion_v = motion_info['motion_v']  # [batch_size * num_frames, motion_v_dim]

        if self.motion_num > 0:
            if self.cfg.consistent_motion:
                motion_words = self.multimotionmap(ws.mean(dim=[0,1]).unsqueeze(0)).reshape(1, self.motion_num, self.cfg.motion_dim)
                motion = motion_words.unsqueeze(1).repeat(motion_v.shape[0], self.num_ws, 1, 1)
            else:
                motion = torch.cat(
                    [ws.repeat_interleave(t.shape[1], dim=0), motion_v.unsqueeze(1).repeat(1, self.num_ws, 1)],
                    dim=2).reshape(-1, self.w_dim + self.motion_v_dim)
                motion = self.multimotionmap(motion).reshape(motion_v.shape[0], self.num_ws, self.motion_num, -1)  # bf,n,m_dim
                motion_words = motion
        else:  # for baseline ablation
            motion = None

        ws = ws.repeat_interleave(t.shape[1], dim=0)  # [batch_size * num_frames, num_ws, w_dim]

        with torch.autograd.profiler.record_function('split_ws'):
            ws = ws.to(torch.float32)
            motion = motion.to(torch.float32)
            w_idx = 0

            for res in self.block_resolutions:
                block = getattr(self, f'b{res}')
                block_ws.append(ws.narrow(1, w_idx, block.num_conv + block.num_torgb))
                block_ms.append(motion.narrow(1, w_idx, block.num_conv + block.num_torgb))
                w_idx += block.num_conv

        x = img = None
        sims = 0.0
        for res, cur_ws, cur_ms in zip(self.block_resolutions, block_ws, block_ms):
            block = getattr(self, f'b{res}')
            x, img, sim = block(x, img, cur_ws, ms=cur_ms, motion_v=motion_v, **block_kwargs)
            sims += sim
        sims =  sims / self.num_ws

        return img, motion_words, sims


# ----------------------------------------------------------------------------

@persistence.persistent_class
class Generator(torch.nn.Module):
    def __init__(self,
                 c_dim,  # Conditioning label (C) dimensionality.
                 w_dim,  # Intermediate latent (W) dimensionality.
                 img_resolution,  # Output resolution.
                 img_channels,  # Number of output color channels.
                 mapping_kwargs={},  # Arguments for MappingNetwork.
                 synthesis_kwargs={},  # Arguments for SynthesisNetwork.
                 cfg={},  # Config
                 ):
        super().__init__()

        self.cfg = cfg
        self.sampling_dict = OmegaConf.to_container(OmegaConf.create({**self.cfg.sampling}))
        self.z_dim = self.cfg.z_dim
        self.c_dim = c_dim
        self.w_dim = w_dim
        self.img_resolution = img_resolution
        self.img_channels = img_channels
        self.synthesis = SynthesisNetwork(w_dim=w_dim, img_resolution=img_resolution, img_channels=img_channels,
                                          cfg=cfg, **synthesis_kwargs)
        self.num_ws = self.synthesis.num_ws
        self.mapping = MappingNetwork(z_dim=self.z_dim, c_dim=c_dim, w_dim=w_dim, num_ws=self.num_ws, **mapping_kwargs)

    def forward(self, z, c, t, truncation_psi=1, truncation_cutoff=None, **synthesis_kwargs):
        assert len(z) == len(c) == len(t), f"Wrong shape: {z.shape}, {c.shape}, {t.shape}"
        assert t.ndim == 2, f"Wrong shape: {t.shape}"

        ws = self.mapping(z, c, truncation_psi=truncation_psi,
                          truncation_cutoff=truncation_cutoff)  # [batch_size, num_ws, w_dim]
        img, motion, sims = self.synthesis(ws, t=t, c=c, **synthesis_kwargs)  # [batch_size * num_frames, c, h, w]

        return img


# ----------------------------------------------------------------------------

@persistence.persistent_class
class DiscriminatorBlock(torch.nn.Module):
    def __init__(self,
                 in_channels,  # Number of input channels, 0 = first block.
                 tmp_channels,  # Number of intermediate channels.
                 out_channels,  # Number of output channels.
                 resolution,  # Resolution of this block.
                 img_channels,  # Number of input color channels.
                 first_layer_idx,  # Index of the first layer.
                 architecture='resnet',  # Architecture: 'orig', 'skip', 'resnet'.
                 activation='lrelu',  # Activation function: 'relu', 'lrelu', etc.
                 resample_filter=[1, 3, 3, 1],  # Low-pass filter to apply when resampling activations.
                 conv_clamp=None,  # Clamp the output of convolution layers to +-X, None = disable clamping.
                 use_fp16=False,  # Use FP16 for this block?
                 fp16_channels_last=False,  # Use channels-last memory format with FP16?
                 freeze_layers=0,  # Freeze-D: Number of layers to freeze.
                 cfg={},  # Main config.
                 ):
        assert architecture in ['orig', 'skip', 'resnet']
        super().__init__()

        self.cfg = cfg
        self.in_channels = in_channels
        self.resolution = resolution
        self.img_channels = img_channels
        self.first_layer_idx = first_layer_idx
        self.architecture = architecture
        self.use_fp16 = use_fp16
        self.channels_last = (use_fp16 and fp16_channels_last)
        self.register_buffer('resample_filter', upfirdn2d.setup_filter(resample_filter))
        self.spectral_norm = self.cfg.spectral_norm

        self.num_layers = 0

        def trainable_gen():
            while True:
                layer_idx = self.first_layer_idx + self.num_layers
                trainable = (layer_idx >= freeze_layers)
                self.num_layers += 1
                yield trainable

        trainable_iter = trainable_gen()
        conv0_in_channels = in_channels if in_channels > 0 else tmp_channels

        if in_channels == 0 or architecture == 'skip':
            self.fromrgb = Conv2dLayer(img_channels, tmp_channels, kernel_size=1, activation=activation,
                                       trainable=next(trainable_iter), conv_clamp=conv_clamp,
                                       channels_last=self.channels_last)

        self.conv0 = Conv2dLayer(conv0_in_channels, tmp_channels, kernel_size=3, activation=activation,
                                 trainable=next(trainable_iter), conv_clamp=conv_clamp,
                                 channels_last=self.channels_last)

        self.conv1 = Conv2dLayer(tmp_channels, out_channels, kernel_size=3, activation=activation, down=2,
                                 trainable=next(trainable_iter), resample_filter=resample_filter, conv_clamp=conv_clamp,
                                 channels_last=self.channels_last)

        if architecture == 'resnet':
            self.skip = Conv2dLayer(conv0_in_channels, out_channels, kernel_size=1, bias=False, down=2,
                                    trainable=next(trainable_iter), resample_filter=resample_filter,
                                    channels_last=self.channels_last)

    def forward(self, x, img, force_fp32=False):
        dtype = torch.float16 if self.use_fp16 and not force_fp32 else torch.float32
        memory_format = torch.channels_last if self.channels_last and not force_fp32 else torch.contiguous_format

        # Input.
        if x is not None:
            misc.assert_shape(x, [None, self.in_channels, self.resolution, self.resolution])
            x = x.to(dtype=dtype, memory_format=memory_format)

        # FromRGB.
        if self.in_channels == 0 or self.architecture == 'skip':
            misc.assert_shape(img, [None, self.img_channels, self.resolution, self.resolution])
            img = img.to(dtype=dtype, memory_format=memory_format)
            y = self.fromrgb(img)
            x = x + y if x is not None else y
            img = upfirdn2d.downsample2d(img, self.resample_filter) if self.architecture == 'skip' else None

        # Main layers.
        if self.architecture == 'resnet':
            y = self.skip(x, gain=np.sqrt(0.5))
            x = self.conv0(x)
            x = self.conv1(x, gain=np.sqrt(0.5))
            x = y.add_(x)
        else:
            if self.spectral_norm:
                x = torch.nn.utils.spectral_norm(self.conv0(x))
                x = torch.nn.utils.spectral_norm(self.conv1(x))
            else:
                x = self.conv0(x)
                x = self.conv1(x)

        assert x.dtype == dtype
        return x, img


# ----------------------------------------------------------------------------

@persistence.persistent_class
class MinibatchStdLayer(torch.nn.Module):
    def __init__(self, group_size, num_channels=1):
        super().__init__()
        self.group_size = group_size
        self.num_channels = num_channels

    def forward(self, x):
        N, C, H, W = x.shape
        with misc.suppress_tracer_warnings():  # as_tensor results are registered as constants
            G = torch.min(torch.as_tensor(self.group_size), torch.as_tensor(N)) if self.group_size is not None else N
        F = self.num_channels
        c = C // F

        y = x.reshape(G, -1, F, c, H,
                      W)  # [GnFcHW] Split minibatch N into n groups of size G, and channels C into F groups of size c.
        y = y - y.mean(dim=0)  # [GnFcHW] Subtract mean over group.
        y = y.square().mean(dim=0)  # [nFcHW]  Calc variance over group.
        y = (y + 1e-8).sqrt()  # [nFcHW]  Calc stddev over group.
        y = y.mean(dim=[2, 3, 4])  # [nF]     Take average over channels and pixels.
        y = y.reshape(-1, F, 1, 1)  # [nF11]   Add missing dimensions.
        y = y.repeat(G, 1, H, W)  # [NFHW]   Replicate over group and pixels.
        x = torch.cat([x, y], dim=1)  # [N(C+1)HW]   Append to input as new channels.
        return x


# ----------------------------------------------------------------------------

@persistence.persistent_class
class DiscriminatorEpilogue(torch.nn.Module):
    def __init__(self,
                 in_channels,  # Number of input channels.
                 cmap_dim,  # Dimensionality of mapped conditioning label, 0 = no label.
                 resolution,  # Resolution of this block.
                 img_channels,  # Number of input color channels.
                 architecture='resnet',  # Architecture: 'orig', 'skip', 'resnet'.
                 mbstd_group_size=4,  # Group size for the minibatch standard deviation layer, None = entire minibatch.
                 mbstd_num_channels=1,  # Number of features for the minibatch standard deviation layer, 0 = disable.
                 activation='lrelu',  # Activation function: 'relu', 'lrelu', etc.
                 conv_clamp=None,  # Clamp the output of convolution layers to +-X, None = disable clamping.
                 cfg={},  # Architecture config.
                 ):
        assert architecture in ['orig', 'skip', 'resnet']
        super().__init__()

        self.cfg = cfg
        self.in_channels = in_channels
        self.cmap_dim = cmap_dim
        self.resolution = resolution
        self.img_channels = img_channels
        self.architecture = architecture

        if architecture == 'skip':
            self.fromrgb = Conv2dLayer(img_channels, in_channels, kernel_size=1, activation=activation)
        self.mbstd = MinibatchStdLayer(group_size=mbstd_group_size,
                                       num_channels=mbstd_num_channels) if mbstd_num_channels > 0 else None
        self.conv = Conv2dLayer(in_channels + mbstd_num_channels, in_channels, kernel_size=3, activation=activation,
                                conv_clamp=conv_clamp)
        self.fc = FullyConnectedLayer(in_channels * (resolution ** 2), self.cfg.motion_dim, activation=activation)
        self.out = FullyConnectedLayer(self.cfg.motion_dim, 1 if cmap_dim == 0 else cmap_dim)

    def forward(self, x, img, cmap, motion=None, force_fp32=False):
        misc.assert_shape(x, [None, self.in_channels, self.resolution, self.resolution])  # [NCHW]
        _ = force_fp32  # unused
        dtype = torch.float32
        memory_format = torch.contiguous_format

        # FromRGB.
        x = x.to(dtype=dtype, memory_format=memory_format)
        if self.architecture == 'skip':
            misc.assert_shape(img, [None, self.img_channels, self.resolution, self.resolution])
            img = img.to(dtype=dtype, memory_format=memory_format)
            x = x + self.fromrgb(img)

        # Main layers.
        if self.mbstd is not None:
            x = self.mbstd(x)

        x = self.conv(x)
        hidden = self.fc(x.flatten(1))
        if motion is not None:
            score = torch.bmm(hidden.unsqueeze(1), motion.repeat_interleave(hidden.shape[0],dim=0).transpose(1,2)) / np.sqrt(hidden.shape[-1]) # bsz, c, num
            attn = F.softmax(score, -1)
            hidden = hidden * (1 + torch.bmm(attn, motion.repeat_interleave(hidden.shape[0],dim=0)).squeeze()) # bsz, c, dim
        x = self.out(hidden)  # [batch_size, out_dim]

        # Conditioning.
        if self.cmap_dim > 0:
            misc.assert_shape(cmap, [None, self.cmap_dim])
            x = (x * cmap).sum(dim=1, keepdim=True) * (1 / np.sqrt(self.cmap_dim))  # [batch_size, 1]

        assert x.dtype == dtype
        return x, hidden


# ----------------------------------------------------------------------------

@persistence.persistent_class
class Discriminator(torch.nn.Module):
    def __init__(self,
                 c_dim,  # Conditioning label (C) dimensionality.
                 img_resolution,  # Input resolution.
                 img_channels,  # Number of input color channels.
                 architecture='resnet',  # Architecture: 'orig', 'skip', 'resnet'.
                 channel_base=32768,  # Overall multiplier for the number of channels.
                 channel_max=512,  # Maximum number of channels in any layer.
                 num_fp16_res=0,  # Use FP16 for the N highest resolutions.
                 conv_clamp=None,  # Clamp the output of convolution layers to +-X, None = disable clamping.
                 cmap_dim=None,  # Dimensionality of mapped conditioning label, None = default.
                 block_kwargs={},  # Arguments for DiscriminatorBlock.
                 mapping_kwargs={},  # Arguments for MappingNetwork.
                 epilogue_kwargs={},  # Arguments for DiscriminatorEpilogue.
                 cfg={},  # Additional config.
                 ):
        super().__init__()

        self.cfg = cfg
        self.c_dim = c_dim
        self.img_resolution = img_resolution
        self.img_resolution_log2 = int(np.log2(img_resolution))
        self.img_channels = img_channels
        self.block_resolutions = [2 ** i for i in range(self.img_resolution_log2, 2, -1)]
        channels_dict = {res: min(channel_base // res, channel_max) for res in self.block_resolutions + [4]}
        fp16_resolution = max(2 ** (self.img_resolution_log2 + 1 - num_fp16_res), 8)

        if cmap_dim is None:
            cmap_dim = channels_dict[4]

        if self.cfg.sampling.num_frames_per_video > 1:
            self.time_encoder = TemporalDifferenceEncoder(self.cfg)
            assert self.time_encoder.get_dim() > 0
        else:
            self.time_encoder = None

        if self.c_dim == 0 and self.time_encoder is None:
            cmap_dim = 0

        common_kwargs = dict(img_channels=img_channels, architecture=architecture, conv_clamp=conv_clamp)
        total_c_dim = c_dim + (0 if self.time_encoder is None else self.time_encoder.get_dim())
        cur_layer_idx = 0

        for res in self.block_resolutions:
            in_channels = channels_dict[res] if res < img_resolution else 0
            tmp_channels = channels_dict[res]
            out_channels = channels_dict[res // 2]

            if res // 2 == self.cfg.concat_res:
                out_channels = out_channels // self.cfg.num_frames_div_factor
            if res == self.cfg.concat_res:
                if self.cfg.motion_diff:
                    in_channels = (in_channels // self.cfg.num_frames_div_factor) * self.cfg.sampling.num_frames_per_video * 2
                else:
                    in_channels = (in_channels // self.cfg.num_frames_div_factor) * self.cfg.sampling.num_frames_per_video

            use_fp16 = (res >= fp16_resolution)
            block = DiscriminatorBlock(in_channels, tmp_channels, out_channels, resolution=res,
                                       first_layer_idx=cur_layer_idx, use_fp16=use_fp16, cfg=self.cfg,**block_kwargs,
                                       **common_kwargs)
            setattr(self, f'b{res}', block)
            cur_layer_idx += block.num_layers

        if self.c_dim > 0 or not self.time_encoder is None:
            self.mapping = MappingNetwork(z_dim=0, c_dim=total_c_dim, w_dim=cmap_dim, num_ws=None, w_avg_beta=None,
                                          **mapping_kwargs)
        self.b4 = DiscriminatorEpilogue(channels_dict[4], cmap_dim=cmap_dim, resolution=4, cfg=self.cfg,
                                        **epilogue_kwargs, **common_kwargs)

    def forward(self, img, c, t, motion=None, **block_kwargs):
        assert len(img) == t.shape[0] * t.shape[1], f"Wrong shape: {img.shape}, {t.shape}"
        assert t.ndim == 2, f"Wrong shape: {t.shape}"

        if self.cfg.motion_diff:
            img = img.view(-1, t.shape[1], *img.shape[1:])
            img_diff = []
            for i in range(t.shape[1]):
                diff = img[:, i] - img[:, (i+1)%t.shape[1]]
                img_diff.append(diff.unsqueeze(0))
            img_diff = torch.cat(img_diff, dim=0).permute(1,0,2,3,4)
            clip = torch.split(img, 1)
            img_diff = torch.split(img_diff, 1)
            img = torch.cat([torch.cat([clip, diff]) for clip, diff in zip(clip, img_diff)])
            img = img.view(-1, *img.shape[2:])

        if not self.time_encoder is None:
            # Encoding the time distances
            t_embs = self.time_encoder(t.view(-1, self.cfg.sampling.num_frames_per_video))  # [batch_size, t_dim]

            # Concatenate `c` and time embeddings
            c = torch.cat([c, t_embs], dim=1)  # [batch_size, c_dim + t_dim]
            c = (c * 0.0) if self.cfg.dummy_c else c  # [batch_size, c_dim + t_dim]

        x = None
        for res in self.block_resolutions:
            block = getattr(self, f'b{res}')
            if res == self.cfg.concat_res:
                # Concatenating the frames
                x = x.view(t.shape[0], -1, *x.shape[1:])  # [batch_size, num_frames, c, h, w]
                x = x.view(x.shape[0], -1, *x.shape[3:])  # [batch_size, num_frames * c, h, w]
            x, img = block(x, img, **block_kwargs)
        cmap = None
        if self.c_dim > 0 or not self.time_encoder is None:
            assert c.shape[1] > 0
        if c.shape[1] > 0:
            cmap = self.mapping(None, c)
        if not self.cfg.dis_attn:
            motion = None
        x, hidden = self.b4(x, img, cmap, motion)
        x = x.squeeze(1)  # [batch_size]

        return {'image_logits': x, 'hidden': hidden}


# ---------------------------------------------------------------------------


# ----------------------------------------------------------------------------
@persistence.persistent_class
class MAL(nn.Module):
    def __init__(self, batch_size):
        super().__init__()
        self.relu = nn.ReLU()
        self.softmax = nn.Softmax(dim=1)
        self.bsz = batch_size

    def alignment(self, gradient, target):

        b, c, h, w = gradient.shape

        t = b // self.bsz

        gradient = gradient.reshape(-1, t, *gradient.shape[1:]).permute(0, 2, 1, 3, 4)  # b,c,t,h,w
        weight = F.adaptive_avg_pool3d(gradient, (t, 1, 1))

        weight_predict = weight * gradient

        predict = self.relu(weight_predict.sum(1))  # sum c -> b,t,h,w

        predict = predict.reshape(self.bsz, t, -1)

        # predict = (predict-torch.min(predict))/torch.max(predict)
        # target = (target-torch.min(weight))/torch.max(target)

        if t > 1:
            # for spatial-temporal alignment
            sim_spa_temp = self.forward_spatial_temporal(predict, target, t)

            # for temporal alignment
            sim_tempo = self.forward_temporal(predict, target, t)

            # cosine similarity equal to l2-normalized mse
            loss_spa_temp = (1 - sim_spa_temp).mean()
            loss_tempo = (1 - sim_tempo).mean()
        else:
            loss_spa_temp = 0.0
            loss_tempo = 0.0

        # for spatial alignment
        dis_spa = self.forward_spatial(predict, target, t)
        loss_spa = dis_spa.mean()

        loss_mal = loss_spa_temp + loss_spa + loss_tempo

        return loss_mal

    def forward_spatial_temporal(self, predict, target, t):
        # target_score = target.reshape(self.bsz, self.t, -1).sum(-1)
        target_score = target.sum(-1)
        target_att = self.softmax(target_score)
        pre_norm = F.normalize(predict, dim=-1)
        target_norm = F.normalize(target, dim=-1)
        sim = (pre_norm * target_norm).sum(-1)
        sim_att = (sim * target_att).sum(-1)
        return sim_att

    def forward_temporal(self, predict, target, t):
        # pre_score = predict.reshape(self.bsz, self.t, -1).sum(-1)
        pre_score = predict.sum(-1)
        motion_score = target.sum(-1)
        pre_norm = F.normalize(pre_score, dim=-1)
        target_norm = F.normalize(motion_score, dim=-1)
        sim = (pre_norm * target_norm).sum(-1)
        return sim

    def forward_spatial(self, predict, target, t):
        # pre_norm = F.normalize(predict.reshape(self.bsz, self.t, -1).mean(1), dim=-1)
        pre_norm = F.normalize(predict.mean(1), dim=-1)
        target_norm = F.normalize(target.mean(1), dim=-1)
        sim = (pre_norm * target_norm).sum(-1)
        return sim

    def forward(self, grad_map, motion_map):
        loss_mal = 0.0
        # misc.assert_shape(motion_map, [None, 3, 256, 256])
        motion_shape = motion_map.shape
        grad_shape = grad_map.shape
        st_map = F.interpolate(motion_map, (grad_shape[-2], grad_shape[-1])).reshape(self.bsz, motion_shape[1], -1)
        loss_l = self.alignment(grad_map, st_map)
        loss_mal += loss_l
        return loss_mal


@persistence.persistent_class
class TSA(torch.nn.Module):
    def __init__(self, in_c):
        super().__init__()

        self.in_c = in_c
        self.q_conv = nn.Conv3d(in_c, in_c // 8, 1, 1, 0)
        self.k_conv = nn.Conv3d(in_c, in_c // 8, 1, 1, 0)
        self.v_conv = nn.Conv3d(in_c, in_c, 1, 1, 0)
        self.gamma = nn.Parameter(torch.zeros(1))
        self.softmax = nn.Softmax(dim=2)

    def forward(self, x):
        b, c, h, w = x.shape
        t = 3 if x.shape[0] % 3 == 0 else 1
        b = b // t
        x = x.reshape(-1, t, c, h, w).permute(0, 2, 1, 3, 4)  # b,c,t,h,w
        key = self.k_conv(x).transpose(2, 1).reshape(b * h * w, -1, t)  # b,h,w,c,t
        query = self.q_conv(x).transpose(2, 1).reshape(b * h * w, -1, t).transpose(2, 1)
        value = self.v_conv(x).transpose(2, 1).reshape(b * h * w, -1, t).transpose(2, 1)
        scores = torch.matmul(query, key)

        attention = self.softmax(scores)
        out = torch.matmul(attention, value)
        out = out.reshape(b, h, w, -1, t).permute(0, 3, 4, 1, 2)  # b,c,t,h,w
        out = self.gamma * out + x
        out = out.permute(0, 2, 1, 3, 4).reshape(-1, c, h, w)  # bt,c,h,w

        return out

def add_mask(img, mask_size):
    pad_size = mask_size
    image_size = img.shape[-1]
    base_size = image_size - pad_size*2
    pad_up = torch.zeros([1, 3, pad_size, image_size]).to(img.device)
    pad_down = torch.zeros([1, 3, pad_size, image_size]).to(img.device)
    pad_left = torch.zeros([1, 3, image_size - pad_size*2, pad_size]).to(img.device)
    pad_right = torch.zeros([1, 3, image_size - pad_size*2, pad_size]).to(img.device)
    base = torch.ones(1, 3, base_size, base_size).to(img.device)
    mask = torch.cat([pad_left, base, pad_right], dim=3).to(img.device)
    mask = torch.cat([pad_up, mask, pad_down], dim=2).to(img.device).to(img.dtype)
    mask = torch.cat(img.shape[0] * [mask])
    return img + mask
