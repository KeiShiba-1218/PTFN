import torch
from torch import nn
from torch.nn import functional as F
from einops import rearrange

from models.axial_transformer import AxialAttention

### https://github.com/megvii-research/NAFNet/blob/main/basicsr/models/archs/NAFNet_arch.py
class LayerNormFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, weight, bias, eps):
        ctx.eps = eps
        N, C, H, W = x.size()
        mu = x.mean(1, keepdim=True)
        var = (x - mu).pow(2).mean(1, keepdim=True)
        y = (x - mu) / (var + eps).sqrt()
        ctx.save_for_backward(y, var, weight)
        y = weight.view(1, C, 1, 1) * y + bias.view(1, C, 1, 1)
        return y

    @staticmethod
    def backward(ctx, grad_output):
        eps = ctx.eps
        N, C, H, W = grad_output.size()
        y, var, weight = ctx.saved_variables
        g = grad_output * weight.view(1, C, 1, 1)
        mean_g = g.mean(dim=1, keepdim=True)
        mean_gy = (g * y).mean(dim=1, keepdim=True)
        gx = 1. / torch.sqrt(var + eps) * (g - y * mean_gy - mean_g)
        return gx, (grad_output * y).sum(dim=3).sum(dim=2).sum(dim=0), grad_output.sum(dim=3).sum(dim=2).sum(
            dim=0), None

class LayerNorm2d(nn.Module):
    def __init__(self, channels, eps=1e-6):
        super(LayerNorm2d, self).__init__()
        self.register_parameter('weight', nn.Parameter(torch.ones(channels)))
        self.register_parameter('bias', nn.Parameter(torch.zeros(channels)))
        self.eps = eps

    def forward(self, x):
        return LayerNormFunction.apply(x, self.weight, self.bias, self.eps)

class SimpleGate(nn.Module):
    def forward(self, x):
        x1, x2 = x.chunk(2, dim=1)
        return x1 * x2

class TemporalShift(nn.Module):
    def __init__(self, n_segment, shift_type, fold_div=3, stride=1):
        super().__init__()
        self.n_segment = n_segment
        self.shift_type = shift_type
        self.fold_div = fold_div
        self.stride = stride 

    def forward(self, x):
        nt, c, h, w = x.size()
        n_batch = nt // self.n_segment
        x = x.view(n_batch, self.n_segment, c, h, w)

        fold = c // self.fold_div # 32/8 = 4
        
        out = torch.zeros_like(x)
        if not 'toFutureOnly' in self.shift_type:
            out[:, :-self.stride, :fold] = x[:, self.stride:, :fold]  # backward (left shift)
            out[:, self.stride:, fold: 2 * fold] = x[:, :-self.stride, fold: 2 * fold]  # forward (right shift)
        else:
            out[:, self.stride:, : 2 * fold] = x[:, :-self.stride, : 2 * fold] # right shift only
        out[:, :, 2 * fold:] = x[:, :, 2 * fold:]  # not shift

        return out.view(nt, c, h, w)

class MemSkip(nn.Module):
    def __init__(self):
        super(MemSkip, self).__init__()
        self.mem_list = []
    def push(self, x):
        if x is not None:
            self.mem_list.insert(0,x)
            return 1
        else:
            return 0
    def pop(self, x):
        if x is not None:
            return self.mem_list.pop()
        else:
            return None

class NAFBlock(nn.Module):
    def __init__(self, c, DW_Expand=2, FFN_Expand=2, drop_out_rate=0.):
        super().__init__()
        dw_channel = c * DW_Expand
        self.conv1 = nn.Conv2d(in_channels=c, out_channels=dw_channel, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        self.conv2 = nn.Conv2d(in_channels=dw_channel, out_channels=dw_channel, kernel_size=3, padding=1, stride=1, groups=dw_channel, bias=True)
        self.conv3 = nn.Conv2d(in_channels=dw_channel // 2, out_channels=c, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        # Simplified Channel Attention
        self.sca = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(in_channels=dw_channel // 2, out_channels=dw_channel // 2, kernel_size=1, padding=0, stride=1,
                      groups=1, bias=True),
        )
        # SimpleGate
        self.sg = SimpleGate()
        ffn_channel = FFN_Expand * c
        self.conv4 = nn.Conv2d(in_channels=c, out_channels=ffn_channel, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        self.conv5 = nn.Conv2d(in_channels=ffn_channel // 2, out_channels=c, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        self.norm1 = LayerNorm2d(c)
        self.norm2 = LayerNorm2d(c)
        self.dropout1 = nn.Dropout(drop_out_rate) if drop_out_rate > 0. else nn.Identity()
        self.dropout2 = nn.Dropout(drop_out_rate) if drop_out_rate > 0. else nn.Identity()
        self.beta = nn.Parameter(torch.zeros((1, c, 1, 1)), requires_grad=True)
        self.gamma = nn.Parameter(torch.zeros((1, c, 1, 1)), requires_grad=True)

    def forward(self, inp):
        x = inp
        x = self.norm1(x)
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.sg(x)
        x = x * self.sca(x)
        x = self.conv3(x)
        x = self.dropout1(x)
        y = inp + x * self.beta
        x = self.conv4(self.norm2(y))
        x = self.sg(x)
        x = self.conv5(x)
        x = self.dropout2(x)
        return y + x * self.gamma

class ShiftNAFBlock(nn.Module):
    def __init__(self, c, DW_Expand=2, FFN_Expand=2, drop_out_rate=0.):
        super().__init__()
        dw_channel = c * DW_Expand
        self.conv1 = nn.Conv2d(in_channels=c, out_channels=dw_channel, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        self.conv2 = nn.Conv2d(in_channels=dw_channel, out_channels=dw_channel, kernel_size=3, padding=1, stride=1, groups=dw_channel, bias=True)
        self.conv3 = nn.Conv2d(in_channels=dw_channel // 2, out_channels=c, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        # Simplified Channel Attention
        self.sca = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(in_channels=dw_channel // 2, out_channels=dw_channel // 2, kernel_size=1, padding=0, stride=1,
                      groups=1, bias=True),
        )
        # SimpleGate
        self.sg = SimpleGate()
        ffn_channel = FFN_Expand * c
        self.conv4 = nn.Conv2d(in_channels=c, out_channels=ffn_channel, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        self.conv5 = nn.Conv2d(in_channels=ffn_channel // 2, out_channels=c, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        self.norm1 = LayerNorm2d(c)
        self.norm2 = LayerNorm2d(c)
        self.dropout1 = nn.Dropout(drop_out_rate) if drop_out_rate > 0. else nn.Identity()
        self.dropout2 = nn.Dropout(drop_out_rate) if drop_out_rate > 0. else nn.Identity()
        self.beta = nn.Parameter(torch.zeros((1, c, 1, 1)), requires_grad=True)
        self.gamma = nn.Parameter(torch.zeros((1, c, 1, 1)), requires_grad=True)

    def forward(self,  left_fold_2fold, center, right):
        fold_div = 8
        n, c, h, w = center.size()
        fold = c//fold_div
        inp = torch.cat([right[:, :fold, :, :], left_fold_2fold, center[:, 2*fold:, :, :]], dim=1)
        x = inp
        x = self.norm1(x)
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.sg(x)
        x = x * self.sca(x)
        x = self.conv3(x)
        x = self.dropout1(x)
        y = inp + x * self.beta
        x = self.conv4(self.norm2(y))
        x = self.sg(x)
        x = self.conv5(x)
        x = self.dropout2(x)

        return y + x * self.gamma

class NAFBlockBBB(nn.Module):
    def __init__(self, c, DW_Expand=2, FFN_Expand=2, drop_out_rate=0.):
        super(NAFBlockBBB, self).__init__()
        self.op = ShiftNAFBlock(c, DW_Expand, FFN_Expand, drop_out_rate)
        self.out_channels = c
        self.left_fold_2fold = None
        self.center = None
        
    def reset(self):
        self.left_fold_2fold = None
        self.center = None
        
    def forward(self, input_right, verbose=False):
        fold_div = 8
        if input_right is not None:
            self.n, self.c, self.h, self.w = input_right.size()
            self.fold = self.c//fold_div
        # Case1: In the start or end stage, the memory is empty
        if self.center is None:
            self.center = input_right
            if input_right is not None:
                if self.left_fold_2fold is None:
                    # In the start stage, the memory and left tensor is empty
                    self.left_fold_2fold = torch.zeros((self.n, self.fold, self.h, self.w), device=torch.device('cuda'))
                if verbose: print("%f+none+%f = none"%(torch.mean(self.left_fold_2fold), torch.mean(input_right)))
            else:
                # in the end stage, both feed in and memory are empty
                if verbose: print("%f+none+none = none"%(torch.mean(self.left_fold_2fold)))
                # print("self.center is None")
            return None
        # Case2: Center is not None, but input_right is None
        elif input_right is None:
            # In the last procesing stage, center is 0
            output =  self.op(self.left_fold_2fold, self.center, torch.zeros((self.n, self.fold, self.h, self.w), device=torch.device('cuda')))
            if verbose: print("%f+%f+none = %f"%(torch.mean(self.left_fold_2fold), torch.mean(self.center), torch.mean(output)))
        else:
            
            output =  self.op(self.left_fold_2fold, self.center, input_right)
            if verbose: print("%f+%f+%f = %f"%(torch.mean(self.left_fold_2fold), torch.mean(self.center), torch.mean(input_right), torch.mean(output)))
        self.left_fold_2fold = self.center[:, self.fold:2*self.fold, :, :]
        self.center = input_right
        return output



class NAFBlockHalf(nn.Module):
    def __init__(self, c, DW_Expand=2, FFN_Expand=2, drop_out_rate=0.):
        super().__init__()
        dw_channel = c * DW_Expand
        self.conv1 = nn.Conv2d(in_channels=c, out_channels=dw_channel, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        self.conv2 = nn.Conv2d(in_channels=dw_channel, out_channels=dw_channel, kernel_size=3, padding=1, stride=1, groups=dw_channel, bias=True)
        self.conv3 = nn.Conv2d(in_channels=dw_channel // 2, out_channels=c, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        # Simplified Channel Attention
        self.sca = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(in_channels=dw_channel // 2, out_channels=dw_channel // 2, kernel_size=1, padding=0, stride=1,
                      groups=1, bias=True),
        )
        # SimpleGate
        self.sg = SimpleGate()
        self.norm1 = LayerNorm2d(c)
        self.dropout1 = nn.Dropout(drop_out_rate) if drop_out_rate > 0. else nn.Identity()
        self.beta = nn.Parameter(torch.zeros((1, c, 1, 1)), requires_grad=True)

    def forward(self, inp):
        x = inp
        x = self.norm1(x)
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.sg(x)
        x = x * self.sca(x)
        x = self.conv3(x)
        x = self.dropout1(x)
        y = inp + x * self.beta
        return y

class ShiftNAFBlockHalf(nn.Module):
    def __init__(self, c, DW_Expand=2, FFN_Expand=2, drop_out_rate=0.):
        super().__init__()
        dw_channel = c * DW_Expand
        self.conv1 = nn.Conv2d(in_channels=c, out_channels=dw_channel, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        self.conv2 = nn.Conv2d(in_channels=dw_channel, out_channels=dw_channel, kernel_size=3, padding=1, stride=1, groups=dw_channel, bias=True)
        self.conv3 = nn.Conv2d(in_channels=dw_channel // 2, out_channels=c, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        # Simplified Channel Attention
        self.sca = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(in_channels=dw_channel // 2, out_channels=dw_channel // 2, kernel_size=1, padding=0, stride=1,
                      groups=1, bias=True),
        )
        # SimpleGate
        self.sg = SimpleGate()
        self.norm1 = LayerNorm2d(c)
        self.dropout1 = nn.Dropout(drop_out_rate) if drop_out_rate > 0. else nn.Identity()
        self.beta = nn.Parameter(torch.zeros((1, c, 1, 1)), requires_grad=True)

    def forward(self,  left_fold_2fold, center, right):
        fold_div = 8
        n, c, h, w = center.size()
        fold = c//fold_div
        inp = torch.cat([right[:, :fold, :, :], left_fold_2fold, center[:, 2*fold:, :, :]], dim=1)
        x = inp
        x = self.norm1(x)
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.sg(x)
        x = x * self.sca(x)
        x = self.conv3(x)
        x = self.dropout1(x)
        y = inp + x * self.beta
        return y

class NAFBlockBBBHalf(nn.Module):
    def __init__(self, c, DW_Expand=2, FFN_Expand=2, drop_out_rate=0.):
        super(NAFBlockBBBHalf, self).__init__()
        self.op = ShiftNAFBlockHalf(c, DW_Expand, FFN_Expand, drop_out_rate)
        self.out_channels = c
        self.left_fold_2fold = None
        self.center = None
        
    def reset(self):
        self.left_fold_2fold = None
        self.center = None
        
    def forward(self, input_right, verbose=False):
        fold_div = 8
        if input_right is not None:
            self.n, self.c, self.h, self.w = input_right.size()
            self.fold = self.c//fold_div
        # Case1: In the start or end stage, the memory is empty
        if self.center is None:
            self.center = input_right
            if input_right is not None:
                if self.left_fold_2fold is None:
                    # In the start stage, the memory and left tensor is empty
                    self.left_fold_2fold = torch.zeros((self.n, self.fold, self.h, self.w), device=torch.device('cuda'))
                if verbose: print("%f+none+%f = none"%(torch.mean(self.left_fold_2fold), torch.mean(input_right)))
            else:
                # in the end stage, both feed in and memory are empty
                if verbose: print("%f+none+none = none"%(torch.mean(self.left_fold_2fold)))
                # print("self.center is None")
            return None
        # Case2: Center is not None, but input_right is None
        elif input_right is None:
            # In the last procesing stage, center is 0
            output =  self.op(self.left_fold_2fold, self.center, torch.zeros((self.n, self.fold, self.h, self.w), device=torch.device('cuda')))
            if verbose: print("%f+%f+none = %f"%(torch.mean(self.left_fold_2fold), torch.mean(self.center), torch.mean(output)))
        else:
            
            output =  self.op(self.left_fold_2fold, self.center, input_right)
            if verbose: print("%f+%f+%f = %f"%(torch.mean(self.left_fold_2fold), torch.mean(self.center), torch.mean(input_right), torch.mean(output)))
        self.left_fold_2fold = self.center[:, self.fold:2*self.fold, :, :]
        self.center = input_right
        return output


class PseudoTemporalConvBlock(nn.Module):
    def __init__(self, c, FFN_Expand=2, drop_out_rate=0.):
        super().__init__()
        self.sg = SimpleGate()
        ffn_channel = FFN_Expand * c
        self.conv1 = nn.Conv2d(in_channels=c, out_channels=ffn_channel, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        self.conv2 = nn.Conv2d(in_channels=ffn_channel // 2, out_channels=c, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        self.norm1 = LayerNorm2d(c)
        self.dropout1 = nn.Dropout(drop_out_rate) if drop_out_rate > 0. else nn.Identity()
        self.beta = nn.Parameter(torch.zeros((1, c, 1, 1)), requires_grad=True)

    def forward(self, inp):
        x = inp
        x = self.conv1(self.norm1(x))
        x = self.sg(x)
        x = self.conv2(x)
        x = self.dropout1(x)
        return inp + x * self.beta

class ShiftPseudoTemporalConvBlock(nn.Module):
    def __init__(self, c, FFN_Expand=2, drop_out_rate=0.):
        super().__init__()
        self.sg = SimpleGate()
        ffn_channel = FFN_Expand * c
        self.conv1 = nn.Conv2d(in_channels=c, out_channels=ffn_channel, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        self.conv2 = nn.Conv2d(in_channels=ffn_channel // 2, out_channels=c, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
        self.norm1 = LayerNorm2d(c)
        self.dropout1 = nn.Dropout(drop_out_rate) if drop_out_rate > 0. else nn.Identity()
        self.beta = nn.Parameter(torch.zeros((1, c, 1, 1)), requires_grad=True)

    def forward(self,  left_fold_2fold, center, right):
        fold_div = 8
        n, c, h, w = center.size()
        fold = c//fold_div
        inp = torch.cat([right[:, :fold, :, :], left_fold_2fold, center[:, 2*fold:, :, :]], dim=1)
        x = inp
        x = self.conv1(self.norm1(x))
        x = self.sg(x)
        x = self.conv2(x)
        x = self.dropout1(x)
        return inp + x * self.beta

class PseudoTemporalConvBlockBBB(nn.Module):
    def __init__(self, c, DW_Expand=2, FFN_Expand=2, drop_out_rate=0.):
        super(self).__init__()
        self.op = ShiftPseudoTemporalConvBlock(c, FFN_Expand, drop_out_rate)
        self.out_channels = c
        self.left_fold_2fold = None
        self.center = None
        
    def reset(self):
        self.left_fold_2fold = None
        self.center = None
        
    def forward(self, input_right, verbose=False):
        fold_div = 8
        if input_right is not None:
            self.n, self.c, self.h, self.w = input_right.size()
            self.fold = self.c//fold_div
        # Case1: In the start or end stage, the memory is empty
        if self.center is None:
            self.center = input_right
            if input_right is not None:
                if self.left_fold_2fold is None:
                    # In the start stage, the memory and left tensor is empty
                    self.left_fold_2fold = torch.zeros((self.n, self.fold, self.h, self.w), device=torch.device('cuda'))
                if verbose: print("%f+none+%f = none"%(torch.mean(self.left_fold_2fold), torch.mean(input_right)))
            else:
                # in the end stage, both feed in and memory are empty
                if verbose: print("%f+none+none = none"%(torch.mean(self.left_fold_2fold)))
                # print("self.center is None")
            return None
        # Case2: Center is not None, but input_right is None
        elif input_right is None:
            # In the last procesing stage, center is 0
            output =  self.op(self.left_fold_2fold, self.center, torch.zeros((self.n, self.fold, self.h, self.w), device=torch.device('cuda')))
            if verbose: print("%f+%f+none = %f"%(torch.mean(self.left_fold_2fold), torch.mean(self.center), torch.mean(output)))
        else:
            
            output =  self.op(self.left_fold_2fold, self.center, input_right)
            if verbose: print("%f+%f+%f = %f"%(torch.mean(self.left_fold_2fold), torch.mean(self.center), torch.mean(input_right), torch.mean(output)))
        self.left_fold_2fold = self.center[:, self.fold:2*self.fold, :, :]
        self.center = input_right
        return output