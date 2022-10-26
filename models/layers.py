from turtle import forward
import torch
from torch import nn
from torch.nn import functional as F
from einops import rearrange

class ChanLayerNorm(nn.Module):
    def __init__(self, dim, eps=1e-5):
        super().__init__()
        self.eps = eps
        self.g = nn.Parameter(torch.ones(1, dim, 1, 1))
        self.b = nn.Parameter(torch.zeros(1, dim, 1, 1))

    def forward(self, x):
        std = torch.var(x, dim = 1, unbiased = False, keepdim = True).sqrt()
        mean = torch.mean(x, dim = 1, keepdim = True)
        return (x - mean) / (std + self.eps) * self.g + self.b

class ShrinkedMultiDeconvHeadAttention(nn.Module):
    def __init__(self, dim, num_heads, r):
        super().__init__()
        self.num_heads = num_heads
        self.norm = ChanLayerNorm(dim)
        self.temperature = nn.Parameter(torch.ones(num_heads, 1, 1, 1))
        self.to_qkv1 = nn.Conv2d(dim, 3*(dim//r), kernel_size=1)
        self.to_qkv2 = nn.Conv2d(3*(dim//r), 3*(dim//r), kernel_size=3, padding=1, groups=3*(dim//r))
        self.to_out = nn.Conv2d(dim//r, dim, kernel_size=1)
        self.merge_heads = lambda x: rearrange(x, 'b (head c) h w -> b head c (h w)', head=num_heads)
    
    def forward(self, x):
        B, C, H, W = x.shape
        a = x
        x = self.norm(x)
        qkv = self.to_qkv2(self.to_qkv1(x))
        q, k, v = qkv.chunk(3, dim=1)
        q,k,v = map(self.merge_heads, [q,k,v])
        q, k = F.normalize(q, dim=-1), F.normalize(k, dim=-1)
        attn = (q @ k.transpose(-2,-1)) #* self.temperature
        attn = attn.softmax(dim=-1)
        out = attn @ v
        out = rearrange(out, 'b head c (h w) -> b (head c) h w', head=self.num_heads, h=H, w=W)
        out = self.to_out(out)
        return out + a

class FeedForwardNetwork(nn.Module):
    def __init__(self, dim, mlp_ratio):
        super().__init__()
        self.norm = ChanLayerNorm(dim)
        self.ln1 = nn.Conv2d(dim, dim*mlp_ratio, kernel_size=1, bias=False)
        self.silu1 = nn.SiLU(inplace=True)
        self.ln2 = nn.Conv2d(dim*mlp_ratio, dim, kernel_size=1, bias=False)
    
    def forward(self, x):
        a = x
        x = self.norm(x)
        x = self.ln1(x)
        x = self.silu1(x)
        x = self.ln2(x)
        return x + a

class TransformerBlock(nn.Module):
    def __init__(self, dim, num_heads, r, mlp_ratio):
        super().__init__()
        self.smdta = ShrinkedMultiDeconvHeadAttention(dim, num_heads, r)
        self.ffn = FeedForwardNetwork(dim, mlp_ratio)
    
    def forward(self, x):
        x = self.smdta(x)
        x = self.ffn(x)
        return x

class SELayer(nn.Module):
    def __init__(self, dim, reduction=4):
        super().__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Sequential(
            nn.Linear(dim, dim//reduction), 
            nn.SiLU(inplace=True),
            nn.Linear(dim//reduction, dim),
            nn.Sigmoid(),
        )

    def forward(self, x):
        B,C,*_ = x.shape
        a = self.avg_pool(x).view(B,C)
        a = self.fc(a).view(B,C,1,1)
        return a*x

class MBConvBlock(nn.Module):
    def __init__(self, in_channels, out_channels, expand_ratio):
        super().__init__()
        self.identity = in_channels==out_channels
        hidden_dim = round(in_channels*expand_ratio)
        self.mbconv = nn.Sequential(
            nn.Conv2d(in_channels, hidden_dim, kernel_size=1, bias=False), 
            nn.BatchNorm2d(hidden_dim), 
            nn.SiLU(inplace=True), 
            nn.Conv2d(hidden_dim, hidden_dim, kernel_size=3, padding=1, bias=False, groups=hidden_dim), 
            nn.BatchNorm2d(hidden_dim), 
            nn.SiLU(inplace=True), 
            SELayer(hidden_dim), 
            nn.Conv2d(hidden_dim, out_channels, kernel_size=1, bias=False),
            nn.BatchNorm2d(out_channels),
        )
    
    def forward(self, x):
        return x + self.mbconv(x) if self.identity else self.mbconv(x)

class FusedMBConvBlock(nn.Module):
    def __init__(self, in_channels, out_channels, expand_ratio):
        super().__init__()
        self.identity = in_channels==out_channels
        hidden_dim = round(in_channels*expand_ratio)
        self.mbconv = nn.Sequential(
            nn.Conv2d(in_channels, hidden_dim, kernel_size=1, bias=False), 
            nn.BatchNorm2d(hidden_dim), 
            nn.SiLU(inplace=True), 
            nn.Conv2d(hidden_dim, hidden_dim, kernel_size=3, padding=1, bias=False), 
            nn.BatchNorm2d(hidden_dim), 
            nn.SiLU(inplace=True), 
            SELayer(hidden_dim), 
            nn.Conv2d(hidden_dim, out_channels, kernel_size=1, bias=False),
            nn.BatchNorm2d(out_channels),
        )
    
    def forward(self, x):
        return x + self.mbconv(x) if self.identity else self.mbconv(x)

class InverseSigmoid(nn.Module):
    def __init__(self, vmin=-6, vmax=6, eps=1e-6):
        super().__init__()
        self.vmin = vmin
        self.vmax = vmax
        self.eps = eps
    
    def forward(self, x):
        return torch.clamp(torch.log(x/(1-x+self.eps)), min=self.vmin, max=self.vmax)

class ConvBNReLU(nn.Module):
    def __init__(self, in_channels, out_channels, stride=1, dilation=1):
        super().__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=stride, padding=1*dilation, dilation=dilation)
        self.bn = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU(inplace=True)
    
    def forward(self, x):
        return self.relu(self.bn(self.conv(x)))

class RConvBNReLU(nn.Module):
    def __init__(self, in_channels, out_channels, stride=1, dilation=1):
        super().__init__()
        self.expand_dim = nn.Conv2d(in_channels, out_channels, kernel_size=1)
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=stride, padding=1*dilation, dilation=dilation)
        self.bn = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU(inplace=True)
    
    def forward(self, x):
        return self.expand_dim(x) + self.relu(self.bn(self.conv(x)))

class RDConvBNReLU(nn.Module):
    def __init__(self, in_channels, out_channels, stride=1, dilation=1):
        super().__init__()
        self.expand_dim = nn.Conv2d(in_channels, out_channels, kernel_size=1)
        self.pconv = nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=stride, padding=1*dilation, dilation=dilation)
        self.dconv = nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=stride, padding=1*dilation, dilation=dilation, groups=out_channels)
        self.bn = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU(inplace=True)
    
    def forward(self, x):
        return self.expand_dim(x) + self.relu(self.bn(self.dconv(self.pconv(x))))

class InputConvBlock(nn.Module):
	def __init__(self, num_in_frames, out_ch):
		super().__init__()
		self.interm_ch = 30
		self.convblock = nn.Sequential(
			nn.Conv2d(num_in_frames*(3+1), num_in_frames*self.interm_ch, \
					  kernel_size=3, padding=1, groups=num_in_frames, bias=False),
			nn.BatchNorm2d(num_in_frames*self.interm_ch),
			nn.ReLU(inplace=True),
			nn.Conv2d(num_in_frames*self.interm_ch, out_ch, kernel_size=3, padding=1, bias=False),
			nn.BatchNorm2d(out_ch),
			nn.ReLU(inplace=True)
		)

	def forward(self, x):
		return self.convblock(x)

class OutputConvBlock(nn.Module):
	def __init__(self, in_ch, out_ch):
		super().__init__()
		self.convblock = nn.Sequential(
			nn.Conv2d(in_ch, in_ch, kernel_size=3, padding=1, bias=False),
			nn.BatchNorm2d(in_ch),
			nn.ReLU(inplace=True),
			nn.Conv2d(in_ch, out_ch, kernel_size=3, padding=1, bias=False)
		)

	def forward(self, x):
		return self.convblock(x)

class ConvBNReLUs(nn.Module):
    def __init__(self, in_channels, hidden_channels, out_channels, depth=2):
        super().__init__()
        self.depth = depth
        for d in range(self.depth):
            in_ch = in_channels if d==0 else hidden_channels
            out_ch = out_channels if d==self.depth-1 else hidden_channels
            setattr(self, f'convbnrelu_{d}', ConvBNReLU(in_ch, out_ch))
    
    def forward(self, x):
        for d in range(self.depth):
            x = getattr(self, f'convbnrelu_{d}')
        return x


class ConvBlock(nn.Module):
    def __init__(self, in_channels, hidden_channels, out_channels, depth, downsample=False, upsample=False):
        super().__init__()
        layers = []
        if downsample:
            layers.append(nn.Conv2d(in_channels, hidden_channels, kernel_size=3, padding=1, stride=2, bias=False))
            layers.append(nn.BatchNorm2d(hidden_channels))
            layers.append(nn.ReLU(inplace=True))
            in_channels = hidden_channels
        
        for d in range(depth):
            layers.append(ConvBNReLU(in_channels, hidden_channels))

        if upsample:
            layers.append(nn.Conv2d(hidden_channels, 4*out_channels, kernel_size=3, padding=1, stride=1, bias=False))
            layers.append(nn.PixelShuffle(2))
        self.layers = nn.Sequential(*layers)
    
    def forward(self, x):
        return self.layers(x)

class UNetBlock(nn.Module):
    def __init__(self, level, in_channels, hidden_channels, out_channels):
        super().__init__()
        self.level = level

        self.input_conv = ConvBNReLU(in_channels, out_channels)

        for l in range(self.level):
            if l==0:
                in_ch, out_ch = out_channels, hidden_channels
            else:
                in_ch, out_ch = hidden_channels, hidden_channels
            
            setattr(self, f'enc_{l}', ConvBNReLU(in_ch, out_ch))
            setattr(self, f'downsample_{l}', nn.MaxPool2d(2, ceil_mode=True))
        
        setattr(self, f'bottom', ConvBNReLU(hidden_channels, hidden_channels, dilation=2))
        
        for l in reversed(range(self.level)):
            if l==0: 
                in_ch, out_ch = hidden_channels, out_channels
            else:
                in_ch, out_ch = hidden_channels, hidden_channels
            
            # setattr(self, f'upsample_{l}', nn.PixelShuffle(2))
            setattr(self, f'dec_{l}', ConvBNReLU(in_ch, out_ch))


    def forward(self, x):
        x = self.input_conv(x)
        a = x
        encoded = []
        for l in range(self.level):
            x = getattr(self, f'enc_{l}')(x)
            encoded.append(x)
            x = getattr(self, f'downsample_{l}')(x)
        
        x = getattr(self, f'bottom')(x)

        for l in reversed(range(self.level)):
            # x = getattr(self, f'upsample_{l}')(x)
            x = F.interpolate(x, scale_factor=2, mode='bilinear', align_corners=False)
            x = getattr(self, f'dec_{l}')(x + encoded[l])
        
        return a + x


class WienerFilter(nn.Module):
    def __init__(self, kernel_size, device=torch.device('cuda')):
        super().__init__()
        self.ksize = kernel_size
        self.kernel = torch.ones((3,1,kernel_size,kernel_size), device=device) / kernel_size**2
    
    def conv2d_with_reflect_pad(self, x, weight, bias=None, stride=1, padding=0, dilation=1, groups=1):
        x = F.pad(x, pad=(padding, padding, padding, padding), mode='reflect')
        x = F.conv2d(x, weight, bias=bias, stride=stride, padding=0, dilation=dilation, groups=groups)
        return x
    
    def forward(self, x, noise_power=None):
        B, C, H, W = x.shape
        local_mean = self.conv2d_with_reflect_pad(x, self.kernel, padding=self.ksize//2, groups=C)
        local_var = self.conv2d_with_reflect_pad(x**2, self.kernel, padding=self.ksize//2, groups=C) - local_mean**2
        if noise_power is None:
            noise_power = local_var.mean(dim=[2,3], keepdim=True)
        x = (x-local_mean) * (1-noise_power/local_var) + local_mean
        x = torch.where(local_var<noise_power, local_mean, x)
        
        return x


# https://github.com/csjliang/LPTN/blob/main/codes/models/archs/LPTN_arch.py
class LaplacianPyramid(nn.Module):
    def __init__(self, level):
        super().__init__()
        self.level = level
        self.kernel = self.gauss_kernel()
    
    def gauss_kernel(self, channels=3, device=torch.device('cuda')):
        kernel = torch.tensor([[1., 4., 6., 4., 1],
                               [4., 16., 24., 16., 4.],
                               [6., 24., 36., 24., 6.],
                               [4., 16., 24., 16., 4.],
                               [1., 4., 6., 4., 1.]])
        kernel /= 256.
        kernel = kernel.repeat(channels, 1, 1, 1)
        kernel = kernel.to(device)
        return kernel
    
    def downsample(self, x):
        return x[:,:,::2,::2]
    
    def upsample(self, x):
        cc = torch.cat([x, torch.zeros(x.shape[0], x.shape[1], x.shape[2], x.shape[3], device=x.device)], dim=3)
        cc = cc.view(x.shape[0], x.shape[1], x.shape[2] * 2, x.shape[3])
        cc = cc.permute(0, 1, 3, 2)
        cc = torch.cat([cc, torch.zeros(x.shape[0], x.shape[1], x.shape[3], x.shape[2] * 2, device=x.device)], dim=3)
        cc = cc.view(x.shape[0], x.shape[1], x.shape[3] * 2, x.shape[2] * 2)
        x_up = cc.permute(0, 1, 3, 2)
        return self.conv_gauss(x_up, 4 * self.kernel)
    
    def conv_gauss(self, img, kernel):
        img = F.pad(img, (2, 2, 2, 2), mode='reflect')
        out = F.conv2d(img, kernel, groups=img.shape[1])
        return out
    
    def pyramid_decom(self, img):
        current = img
        pyr = []
        for i in range(self.level):
            filtered = self.conv_gauss(current, self.kernel)
            down = self.downsample(filtered)
            up = self.upsample(down)
            if up.shape[2] != current.shape[2] or up.shape[3] != current.shape[3]:
                up = F.interpolate(up, size=(current.shape[2], current.shape[3]))
            diff = current - up
            _,_,h,w = diff.shape
            pyr.append(diff)
            current = down
        _,_,h,w = current.shape
        pyr.append(current)
        return pyr

    def pyramid_recons(self, pyr):
        image = pyr[-1]
        for level in reversed(pyr[:-1]):
            up = self.upsample(image)
            if up.shape[2] != level.shape[2] or up.shape[3] != level.shape[3]:
                up = F.interpolate(up, size=(level.shape[2], level.shape[3]))
            image = up + level
        _,c,h,w = image.shape
        return image