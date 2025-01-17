import os
import json
from typing import Any

import torch
import numpy as np
from einops import rearrange
from PIL import Image

import imageio
from tqdm import tqdm  # Import tqdm

from unet_sd import UNetSD
from autoencoder import AutoencoderKL
from clip_embedder import FrozenOpenCLIPEmbedder
from diffusion import GaussianDiffusion, beta_schedule

from PIL import Image
import torchvision.utils as vutils
import torchvision.transforms.functional as TF
class SDVideo:
    def __init__(self, model_path: str, device: str | torch.device = torch.device('cpu')):
        #TODO fix the loading and progress bars
        print("Loading model into memory...")
        self.device = torch.device(device)
        with open(os.path.join(model_path, 'configuration.json'), 'r') as f:
            self.config: dict[str, Any] = json.load(f)
        cfg = self.config['model']['model_cfg']
        cfg['temporal_attention'] = True if cfg[
            'temporal_attention'] == 'True' else False

        self.unet: UNetSD = UNetSD(
                in_dim = cfg['unet_in_dim'],
                dim = cfg['unet_dim'],
                y_dim = cfg['unet_y_dim'],
                context_dim = cfg['unet_context_dim'],
                out_dim = cfg['unet_out_dim'],
                dim_mult = cfg['unet_dim_mult'],
                num_heads = cfg['unet_num_heads'],
                head_dim = cfg['unet_head_dim'],
                num_res_blocks = cfg['unet_res_blocks'],
                attn_scales = cfg['unet_attn_scales'],
                dropout = cfg['unet_dropout'],
                temporal_attention = cfg['temporal_attention']
        )
        self.unet.load_state_dict(
                torch.load(os.path.join(model_path, self.config['model']['model_args']['ckpt_unet'])),
                strict = True
        )
        self.unet = self.unet.eval().requires_grad_(False)
        self.unet.to(self.device)

        betas = beta_schedule(
                'linear_sd',
                cfg['num_timesteps'],
                init_beta=0.00085,
                last_beta=0.0120
        )
        self.diffusion = GaussianDiffusion(
                betas = betas,
                mean_type = cfg['mean_type'],
                var_type = cfg['var_type'],
                loss_type = cfg['loss_type'],
                rescale_timesteps = False
        )

        ddconfig = {
                'double_z': True,
                'z_channels': 4,
                'resolution': 256,
                'in_channels': 3,
                'out_ch': 3,
                'ch': 128,
                'ch_mult': [1, 2, 4, 4],
                'num_res_blocks': 2,
                'attn_resolutions': [],
                'dropout': 0.0
        }
        self.vae: AutoencoderKL = AutoencoderKL(
                ddconfig,
                4,
                os.path.join(model_path, self.config['model']['model_args']['ckpt_autoencoder'])
        )
        self.vae = self.vae.eval().requires_grad_(False)
        self.vae.to(self.device)

        self.text_encoder: FrozenOpenCLIPEmbedder = FrozenOpenCLIPEmbedder(
                version = os.path.join(model_path, self.config['model']['model_args']['ckpt_clip']),
                layer = 'penultimate'
        )
        self.text_encoder = self.text_encoder.eval().requires_grad_(False)
        self.text_encoder.to(self.device)

    def __call__(self, text: str, text_neg: str = '', max_frames: int = 16, initial_alpha: float = 0.23, ratio: float = 0.8, image_path: str = "input.png", output_file_path: str = "output.webm", fps: int = 24) -> str:
        #print("Preprocessing...")
        text_emb, text_emb_neg = self.preprocess(text, text_neg)
        
        print("Processing: "+text)
        y = self.process(text_emb, text_emb_neg, image_path, initial_alpha, ratio, max_frames)
        
        #print("Postprocessing...")
        out = self.postprocess(y)

        self.save_webm(out, output_file_path, fps)
        
        return "complete"

    def preprocess(self, text: str, text_neg: str = '') -> tuple[torch.Tensor, torch.Tensor]:
        text_emb = self.text_encoder(tqdm([text], desc="Encoding text", ncols=100))
        text_emb_neg = self.text_encoder(tqdm([text_neg], desc="Encoding negative text", ncols=100))
        return text_emb, text_emb_neg

    def postprocess(self, x: torch.Tensor) -> dict[str, list[np.ndarray]]:
        return tensor2vid(x)
    
    #needs to return tensor of shape (1, 4, self.max_frames, latent_w, latent_h)
    def preprocess_image_RGB(self, image_path, output_size, device, max_frames: int = 16):
        image = Image.open(image_path).convert('RGB')
        image = image.resize(output_size)
        image = TF.to_tensor(image).unsqueeze(0)
        
        #TODO: no matter what order the channels, the green is always red
        image = image[:, [0,1,2], :, :]

        latent_w, latent_h = output_size
        final_tensor = torch.zeros((1, 4, max_frames, latent_w, latent_h), device=device)
        
        for j in range(max_frames):  # self.max_frames frames
            final_tensor[0, :3, j, :, :] = image

        return final_tensor
    
    def process(self, text_emb: torch.Tensor, text_emb_neg: torch.Tensor, image_path: str = None, initial_alpha: float = 0.23, ratio: float = 0.8, max_frames: int = 16) -> torch.Tensor:
        context = torch.cat([text_emb_neg, text_emb], dim=0).to(self.device)
        # synthesis
        with torch.no_grad():
            num_sample = 1  # here let b = 1
            latent_h, latent_w = 32, 32

            # Create noise tensor shape (1, 4, self.max_frames, latent_h, latent_w)
            input_noise_tensor = torch.randn(num_sample, 4, max_frames, latent_h, latent_w).to(self.device)

            # Load and preprocess the image
            image_tensor = self.preprocess_image_RGB(image_path, (latent_w, latent_h), self.device, max_frames)
            print("Image weight "+str(initial_alpha)+"x"+str(ratio)+" Tensor size: "+str(image_tensor.size()))
            print("Processing video "+str(max_frames)+" frames...")

            ## Calculate mean and std for the image tensor
            image_mean = image_tensor.mean(dim=[0, 2, 3], keepdim=True)
            image_std = image_tensor.std(dim=[0, 2, 3], keepdim=True)

            # Normalize the image tensor
            normalized_image_tensor = (image_tensor - image_mean) / image_std

            # Create a new tensor with the same shape as input_noise_tensor
            combined_tensor = input_noise_tensor.clone()
            
            # Update the combined tensor with image data for the first three channels (RGB) for all frames
            combined_tensor[:, :3, :, :, :] = normalized_image_tensor[:, :3, :, :, :]
            
            # Add noise to the first three channels of the combined tensor
            noise_tensor = torch.randn_like(combined_tensor)
            
            # Blend noise tensor and image tensor using a blending factor (alpha) that changes per frame
            initial_alpha = initial_alpha  # Initial blending factor (0 <= alpha <= 1)
            ratio = ratio  # Define the ratio to reduce alpha per frame
            alphas = [initial_alpha * (ratio ** i) for i in range(max_frames)]
            # Create the blended tensor
            blended_tensors = []
            for i in range(max_frames):
                alpha = alphas[i]
                blended_frame = alpha * combined_tensor[:, :, i, :, :] + (1 - alpha) * noise_tensor[:, :, i, :, :]
                blended_tensors.append(blended_frame.unsqueeze(2))
            blended_tensor = torch.cat(blended_tensors, dim=2)

            # Calculate mean and std for the blended tensor
            blended_mean = blended_tensor.mean(dim=[0, 2, 3], keepdim=True)
            blended_std = blended_tensor.std(dim=[0, 2, 3], keepdim=True)
            # Normalize the blended tensor
            normalized_blended_tensor = (blended_tensor - blended_mean) / blended_std
            
            # Save noise preview
            self.save_noise(normalized_blended_tensor, max_frames)

            with torch.autocast(self.device.type, enabled=True):
                x0 = self.diffusion.ddim_sample_loop(
                    noise=normalized_blended_tensor,
                    model=self.unet,
                    model_kwargs=[{
                        'y': context[1].unsqueeze(0).repeat(num_sample, 1, 1)
                    }, {
                        'y': context[0].unsqueeze(0).repeat(num_sample, 1, 1)
                    }],
                    guide_scale=9.0,
                    ddim_timesteps=50,
                    eta=0.0
                )

                scale_factor = 0.18215
                video_data = 1. / scale_factor * x0
                bs_vd = video_data.shape[0]
                video_data = rearrange(video_data, 'b c f h w -> (b f) c h w')
                self.vae.to(self.device)
                video_data = self.vae.decode(video_data)
                video_data = rearrange(
                    video_data, '(b f) c h w -> b c f h w', b=bs_vd)
        return video_data

    def pil_img_to_torch(self, pil_img, half=False):
        image = np.array(pil_img).astype(np.float32) / 255.0
        image = rearrange(torch.from_numpy(image), 'h w c -> c h w')
        if half:
            image = image.half()
        return (2.0 * image - 1.0).unsqueeze(0)
    
    def save_noise(self, input_noise_tensor: torch.Tensor, max_frames: int = 16):
        # Save preview image for each frame
        preview_dir = os.getcwd()
        for j in range(max_frames):
            frame_preview_path = os.path.join(preview_dir, f'noise/preview_frame_{j}.png')
            frame_data = input_noise_tensor[0, :3, j, ...].squeeze()  # Get the first 3 channels (RGB)
            vutils.save_image(frame_data, frame_preview_path, normalize=True)

    def print_pixel_values(self, tensor: torch.Tensor, tensor_name: str, num_channels: int = 4, frame_idx: int = 0):
        print(f"Pixel values for {tensor_name}:")
        for ch in range(num_channels):
            print(f"Channel {ch}, Frame {frame_idx}:")
            print(tensor[0, ch, frame_idx, :, :])

    def save_webm(self, images: torch.Tensor, file_path: str, fps: int = 24) -> None:
        print("Saving video as "+file_path)
        images = images.mul(255).round().clamp(0, 255).to(dtype=torch.uint8, device='cpu').numpy()
        frames = [Image.fromarray(x) for x in images]

        # Save the video as a WebM file
        with imageio.get_writer(file_path, format='WEBM', mode='I', fps=fps, codec='vp9') as writer:
            for frame in frames:
                # Convert the PIL.Image object to a NumPy array
                frame_array = np.array(frame)
                writer.append_data(frame_array)

    def process_multiline_prompt(self, multiline_prompt: str, image_path: str, max_frames: int = 16, initial_alpha: float = 0.23, ratio: float = 0.8, output_file_path: str = "output.webm", fps: int = 16) -> str:
        # Split the multiline_prompt into individual lines
        prompts = multiline_prompt.split("\n")

        # Initialize a list to store the frames of all videos
        all_frames = []

        # Iterate through each prompt
        for prompt in prompts:
            # Generate a video for the current prompt
            self.__call__(prompt, max_frames=max_frames, initial_alpha=initial_alpha, ratio=ratio, image_path=image_path, output_file_path=output_file_path, fps=fps)

            # Load the generated video
            video_frames = imageio.mimread(output_file_path)

            # Append the frames of the current video to the all_frames list
            all_frames.extend(video_frames)

            # Update the image_path for the next prompt to use the last frame of the current video
            last_frame = Image.fromarray(video_frames[-1])
            last_frame.save("temp_last_frame.png")
            image_path = "temp_last_frame.png"

        # Save the concatenated video
        with imageio.get_writer(output_file_path, format='WEBM', mode='I', fps=fps, codec='vp9') as writer:
            for frame in all_frames:
                writer.append_data(frame)

        # Remove the temporary last frame image
        if os.path.exists("temp_last_frame.png"):
            os.remove("temp_last_frame.png")

        return "complete"

def tensor2vid(
        video: torch.Tensor,
        mean: tuple[float, float, float] | float = (0.5, 0.5, 0.5),
        std: tuple[float, float, float] | float = (0.5, 0.5, 0.5)
) -> torch.Tensor:
    if isinstance(mean, float):
        mean = (mean,) * 3
    if isinstance(std, float):
        std = (std,) * 3
    mean = torch.tensor(mean, device = video.device).reshape(1, -1, 1, 1, 1)  # n c f h w
    std = torch.tensor(std, device=video.device).reshape(1, -1, 1, 1, 1)  # n c f h w
    video = video.mul_(std).add_(mean)
    images = rearrange(video, 'i c f h w -> f h (i w) c')  # f h w c
    return images

def save_webm(images: torch.Tensor, dir_path: str, file_name: str, fps: int = 24) -> None:
    print("Saving video as "+file_name)
    images = images.mul(255).round().clamp(0, 255).to(dtype=torch.uint8, device='cpu').numpy()
    frames = [Image.fromarray(x) for x in images]

    # Join the directory and file name to form the full file path
    file_path = os.path.join(dir_path, file_name)

    # Create the output directory if it doesn't exist
    os.makedirs(dir_path, exist_ok=True)

    # Save the video as a WebM file
    with imageio.get_writer(file_path, format='WEBM', mode='I', fps=fps, codec='vp9') as writer:
        for frame in frames:
            # Convert the PIL.Image object to a NumPy array
            frame_array = np.array(frame)
            writer.append_data(frame_array)