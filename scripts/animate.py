import argparse
import datetime
import inspect
import os
from omegaconf import OmegaConf

import torch

import diffusers
from diffusers import AutoencoderKL, DDIMScheduler

from tqdm.auto import tqdm
from transformers import CLIPTextModel, CLIPTokenizer

from animatediff.models.unet import UNet3DConditionModel
from animatediff.pipelines.pipeline_animation import AnimationPipeline
from animatediff.utils.util import save_videos_grid
from animatediff.utils.convert_from_ckpt import convert_ldm_unet_checkpoint, convert_ldm_clip_checkpoint, convert_ldm_vae_checkpoint
from animatediff.utils.convert_lora_safetensor_to_diffusers import convert_lora
from diffusers.utils.import_utils import is_xformers_available

from einops import rearrange, repeat

import csv, pdb, glob
from safetensors import safe_open
import math
from pathlib import Path
import shutil

import numpy as np
from PIL import Image

def save_individual_frames(video_array, save_path, filename_prefix, save_last_frame=True):
    # video_array shape: (batch, channels, frames, height, width)
    # We assume batch size is 1 for simplicity
    video = video_array[0].transpose(1, 2, 3, 0)  # (frames, height, width, channels)
    
    for i, frame in enumerate(video):
        # Convert from float [0,1] to uint8 [0,255]
        frame_uint8 = (frame * 255).astype(np.uint8)
        
        # Convert to PIL Image and save
        image = Image.fromarray(frame_uint8)
        image.save(f"{save_path}/{filename_prefix}_frame_{i:04d}.png")

    print(f"Saved {len(video)} frames to {save_path}")

    # Save the last frame separately
    if save_last_frame:
        last_frame = video[-1]
        last_frame_uint8 = (last_frame * 255).astype(np.uint8)
        last_image = Image.fromarray(last_frame_uint8)
        last_frame_path = f"{save_path}/st.png"
        last_image.save(last_frame_path)
        print(f"Saved last frame to {last_frame_path}")


def read_prompts_from_file(file_path):
    with open(file_path, 'r') as file:
        prompts = [line.strip() for line in file if line.strip()]
    return prompts


def main(args):
    *_, func_args = inspect.getargvalues(inspect.currentframe())
    func_args = dict(func_args)
    videofilename=args.filename
    time_str = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    savedir = f"samples/{Path(args.config).stem}-{time_str}"
    os.makedirs(savedir)
    inference_config = OmegaConf.load(args.inference_config)

    config  = OmegaConf.load(args.config)
    samples = []
    
    sample_idx = 0
    for model_idx, (config_key, model_config) in enumerate(list(config.items())):
        
        motion_modules = model_config.motion_module
        motion_modules = [motion_modules] if isinstance(motion_modules, str) else list(motion_modules)
        for motion_module in motion_modules:
        
            ### >>> create validation pipeline >>> ###
            tokenizer    = CLIPTokenizer.from_pretrained(args.pretrained_model_path, subfolder="tokenizer")
            text_encoder = CLIPTextModel.from_pretrained(args.pretrained_model_path, subfolder="text_encoder")
            vae          = AutoencoderKL.from_pretrained(args.pretrained_model_path, subfolder="vae")            
            unet         = UNet3DConditionModel.from_pretrained_2d(args.pretrained_model_path, subfolder="unet", unet_additional_kwargs=OmegaConf.to_container(inference_config.unet_additional_kwargs))

            if is_xformers_available(): unet.enable_xformers_memory_efficient_attention()
            else: assert False

            pipeline = AnimationPipeline(
                vae=vae, text_encoder=text_encoder, tokenizer=tokenizer, unet=unet,
                scheduler=DDIMScheduler(**OmegaConf.to_container(inference_config.noise_scheduler_kwargs)),
            ).to("cuda")

            # 1. unet ckpt
            # 1.1 motion module
            motion_module_state_dict = torch.load(motion_module, map_location="cpu")
            if "global_step" in motion_module_state_dict: 
              func_args.update({"global_step": motion_module_state_dict["global_step"]})

            # Extract only the model weights
            if "state_dict" in motion_module_state_dict:
              model_weights = motion_module_state_dict["state_dict"]
            else:
              model_weights = motion_module_state_dict

            missing, unexpected = pipeline.unet.load_state_dict(model_weights, strict=False)
            if len(unexpected) > 0:
              print(f"Warning: {len(unexpected)} unexpected keys in motion module were not loaded:")
              print(unexpected)
                
            # 1.2 T2I
            if model_config.path != "":
                if model_config.path.endswith(".ckpt"):
                    state_dict = torch.load(model_config.path)
                    pipeline.unet.load_state_dict(state_dict)
                    
                elif model_config.path.endswith(".safetensors"):
                    state_dict = {}
                    with safe_open(model_config.path, framework="pt", device="cpu") as f:
                        for key in f.keys():
                            state_dict[key] = f.get_tensor(key)
                            
                    is_lora = all("lora" in k for k in state_dict.keys())
                    if not is_lora:
                        base_state_dict = state_dict
                    else:
                        base_state_dict = {}
                        with safe_open(model_config.base, framework="pt", device="cpu") as f:
                            for key in f.keys():
                                base_state_dict[key] = f.get_tensor(key)                
                    
                    # vae
                    converted_vae_checkpoint = convert_ldm_vae_checkpoint(base_state_dict, pipeline.vae.config)
                    pipeline.vae.load_state_dict(converted_vae_checkpoint)
                    # unet
                    converted_unet_checkpoint = convert_ldm_unet_checkpoint(base_state_dict, pipeline.unet.config)
                    pipeline.unet.load_state_dict(converted_unet_checkpoint, strict=False)
                    # text_model
                    pipeline.text_encoder = convert_ldm_clip_checkpoint(base_state_dict)
                    
                    # import pdb
                    # pdb.set_trace()
                    if is_lora:
                        pipeline = convert_lora(pipeline, state_dict, alpha=model_config.lora_alpha)
                    
                    # additional networks
                    if hasattr(model_config, 'additional_networks') and len(model_config.additional_networks) > 0:
                        for lora_weights in model_config.additional_networks:
                            add_state_dict = {}
                            (lora_path, lora_alpha) = lora_weights.split(':')
                            print(f"loading lora {lora_path} with weight {lora_alpha}")
                            lora_alpha = float(lora_alpha.strip())
                            with safe_open(lora_path.strip(), framework="pt", device="cpu") as f:
                                for key in f.keys():
                                    add_state_dict[key] = f.get_tensor(key)
                            pipeline = convert_lora(pipeline, add_state_dict, alpha=lora_alpha)
                            
            pipeline.to("cuda")
            ### <<< create validation pipeline <<< ###

            #prompts      = model_config.prompt
            prompts = read_prompts_from_file(args.prompts_file)

            n_prompts    = list(model_config.n_prompt) * len(prompts) if len(model_config.n_prompt) == 1 else model_config.n_prompt
            init_image   = model_config.init_image if hasattr(model_config, 'init_image') else None

            random_seeds = model_config.get("seed", [-1])
            random_seeds = [random_seeds] if isinstance(random_seeds, int) else list(random_seeds)
            random_seeds = random_seeds * len(prompts) if len(random_seeds) == 1 else random_seeds
            
            config[config_key].random_seed = []
            for prompt_idx, (prompt, n_prompt, random_seed) in enumerate(zip(prompts, n_prompts, random_seeds)):
                
                # manually set random seed for reproduction
                if random_seed != -1: torch.manual_seed(random_seed)
                else: torch.seed()
                config[config_key].random_seed.append(torch.initial_seed())
                
                print(f"current seed: {torch.initial_seed()}")
                print(f"sampling {prompt} ...")
                sample = pipeline(
                    prompt,
                    init_image          = init_image,
                    negative_prompt     = n_prompt,
                    num_inference_steps = model_config.steps,
                    guidance_scale      = model_config.guidance_scale,
                    width               = args.W,
                    height              = args.H,
                    video_length        = args.L,
                ).videos
                samples.append(sample)
                # init_image is just a path and filename loaded in prepare_latents() in animation_pipeline around line 292
                
                prompt = "-".join((prompt.replace("/", "").split(" ")[:10]))
                
                save_videos_grid(sample, f"{savedir}/sample/{videofilename}.gif")
                print(f"save to {savedir}/sample/{videofilename}.gif")

                frames_dir = f"{savedir}/frames/{videofilename}"
                os.makedirs(frames_dir, exist_ok=True)
                save_individual_frames(sample.cpu().numpy(), frames_dir, videofilename)
                
                sample_idx += 1

    samples = torch.concat(samples)
    save_videos_grid(samples, f"{savedir}/sample.gif", n_rows=4)

    OmegaConf.save(config, f"{savedir}/config.yaml")
    if init_image is not None:
        shutil.copy(init_image, f"{savedir}/{sample_idx}-init_image.jpg")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pretrained_model_path", type=str, default="models/StableDiffusion/stable-diffusion-v1-5",)
    parser.add_argument("--inference_config",      type=str, default="configs/inference/inference.yaml")    
    parser.add_argument("--config",                type=str, required=True)
    #parser.add_argument("--prompts_file", type=str, required=True, help="Path to the text file containing prompts")
    # this is for splitting the prompts out internally, implemented for now at the ipynb cell level
    
    parser.add_argument("--L", type=int, default=16 )
    parser.add_argument("--W", type=int, default=512)
    parser.add_argument("--H", type=int, default=512)
    parser.add_argument("--filename", type=str, default="0000") # use in main to save in specific file name
    args = parser.parse_args()
    main(args)
