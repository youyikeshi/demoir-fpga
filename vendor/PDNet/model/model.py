import torch
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torchvision
from utils.loss_util import *
from utils.common import *
from torch.nn.parameter import Parameter
from functools import partial
import time

# device1 is only used by the training closure (it is where the teacher model
# lives). test.py never passes it, so default it to None instead of making it a
# required positional argument as in the original release.
def model_fn_decorator(loss_fn, device, device1=None, mode='train'):
    def test_model_fn(args, data, model, save_path, compute_metrics):
        # prepare input and forward
        number = data['number']
        cur_psnr = 0.0
        cur_ssim = 0.0
        cur_lpips = 0.0

        in_img = data['in_img'].to(device)
        label = data['label'].to(device)
        b, c, h, w = in_img.size()

        # pad image such that the resolution is a multiple of 32
        w_pad = (math.ceil(w/32)*32 - w) // 2
        h_pad = (math.ceil(h/32)*32 - h) // 2
        w_odd_pad = w_pad
        h_odd_pad = h_pad
        if w % 2 == 1:
            w_odd_pad += 1
        if h % 2 == 1:
            h_odd_pad += 1

        in_img = img_pad(in_img, w_pad=w_pad, h_pad=h_pad, w_odd_pad=w_odd_pad, h_odd_pad=h_odd_pad)

        with torch.no_grad():
            st = time.time()
            out_1, out_2, out_3, fm = model(in_img)
            cur_time = time.time()-st
            if h_pad != 0:
               out_1 = out_1[:, :, h_pad:-h_odd_pad, :]
            if w_pad != 0:
               out_1 = out_1[:, :, :, w_pad:-w_odd_pad]

        if args.EVALUATION_METRIC:
            cur_lpips, cur_psnr, cur_ssim = compute_metrics.compute(out_1, label)

        # save images
        if args.SAVE_IMG:
            out_save = out_1.detach().cpu()
            torchvision.utils.save_image(out_save, save_path + '/' + 'test_%s' % number[0] + '.%s' % args.SAVE_IMG)

        return cur_psnr, cur_ssim, cur_lpips, cur_time

    def model_fn(args, data, model, T_model, iters):
        model.train()
        # prepare input and forward
        in_img = data['in_img'].to(device)
        label = data['label'].to(device)
        out_1, out_2, out_3, feature_map = model(in_img)

        if T_model is None:
            # Distillation disabled. Feed the student's own detached outputs in
            # the teacher slots: the loss computes
            #     a = 1 if l1_s > l1_t else 0
            # and with Tout_x == out_x.detach() we get l1_t == l1_s, so a == 0
            # and the 40*a*feature_loss term drops out. The empty feature list
            # also skips the DWT comparison entirely, so no extra compute.
            loss = loss_fn(out_1, out_2, out_3, label, feature_map,
                           out_1.detach(), out_2.detach(), out_3.detach(), [])
        else:
            T_in_img = data['in_img'].to(device1)
            Tout_1, Tout_2, Tout_3, T_feature_map = T_model(T_in_img)
            T_fm = []
            for fm in (T_feature_map):
                fm = fm.to(device)
                T_fm.append(fm)
            Tout_1 = Tout_1.to(device)
            loss = loss_fn(out_1, out_2, out_3, label, feature_map, Tout_1, Tout_2, Tout_3, T_fm)

        # save images
        if iters % args.SAVE_ITER == (args.SAVE_ITER - 1):
            in_save = in_img.detach().cpu()
            out_save = out_1.detach().cpu()
            gt_save = label.detach().cpu()
            res_save = torch.cat((in_save, out_save, gt_save), 3)
            save_number = (iters + 1) // args.SAVE_ITER
            torchvision.utils.save_image(res_save,
                                         args.VISUALS_DIR + '/visual_x%04d_' % args.SAVE_ITER + '%05d' % save_number + '.jpg')


        return loss

    if mode == 'test':
        fn = test_model_fn
    else:
        fn = model_fn
    return fn