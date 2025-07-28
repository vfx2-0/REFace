import argparse, os, sys, glob
import cv2
import torch
import numpy as np
from omegaconf import OmegaConf
# import pandas as pd
from PIL import Image
from tqdm import tqdm, trange
# from imwatermark import WatermarkEncoder
from itertools import islice
from einops import rearrange
from torchvision.utils import make_grid
import time
from pytorch_lightning import seed_everything
from torch import autocast
from contextlib import contextmanager, nullcontext
import torchvision
from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.plms import PLMSSampler
import torchvision.transforms as transforms
import albumentations as A
# from moviepy.video.io.ImageSequenceClip import ImageSequenceClip
# from moviepy.editor import AudioFileClip, VideoFileClip
# import proglog

from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker
from transformers import AutoFeatureExtractor

from src.utils.alignment import crop_faces, calc_alignment_coefficients, crop_faces_from_image

# from ldm.data.test_bench_dataset import COCOImageDataset
# from ldm.data.test_bench_dataset import CelebAdataset,FFHQdataset
from ldm.data.video_swap_dataset import VideoDataset
# import clip
from torchvision.transforms import Resize


from PIL import Image
from torchvision.transforms import PILToTensor


from pretrained.face_parsing.face_parsing_demo import init_faceParsing_pretrained_model, faceParsing_demo, vis_parsing_maps
# from dift.src.models.dift_sd import SDFeaturizer
# from dift.src.utils.visualization import Demo


# import matplotlib.pyplot as plt
import torch.nn as nn

# cos = nn.CosineSimilarity(dim=0)
import numpy as np  

# load safety model
safety_model_id = "CompVis/stable-diffusion-safety-checker"
safety_feature_extractor = AutoFeatureExtractor.from_pretrained(safety_model_id)
safety_checker = StableDiffusionSafetyChecker.from_pretrained(safety_model_id)

#set cuda device 
# os.environ["CUDA_VISIBLE_DEVICES"] = "1"

def get_tensor(normalize=True, toTensor=True):
    transform_list = []
    if toTensor:
        transform_list += [torchvision.transforms.ToTensor()]

    if normalize:
        transform_list += [torchvision.transforms.Normalize((0.5, 0.5, 0.5),
                                                (0.5, 0.5, 0.5))]
    return torchvision.transforms.Compose(transform_list)

def get_tensor_clip(normalize=True, toTensor=True):
    transform_list = []
    if toTensor:
        transform_list += [torchvision.transforms.ToTensor()]

    if normalize:
        transform_list += [torchvision.transforms.Normalize((0.48145466, 0.4578275, 0.40821073),
                                                (0.26862954, 0.26130258, 0.27577711))]
    return torchvision.transforms.Compose(transform_list)

def crop_and_align_face(target_files):
    image_size = 1024
    scale = 1.0
    center_sigma = 0
    xy_sigma = 0
    use_fa = False
    
    print('Aligning images')
    crops, orig_images, quads = crop_faces(image_size, target_files, scale, center_sigma=center_sigma, xy_sigma=xy_sigma, use_fa=use_fa)
    
    inv_transforms = [
        calc_alignment_coefficients(quad + 0.5, [[0, 0], [0, image_size], [image_size, image_size], [image_size, 0]])
        for quad in quads
    ]
    
    return crops, orig_images, quads, inv_transforms

def crop_and_align_face_img(frame):
    image_size = 1024
    scale = 1.0
    center_sigma = 0
    xy_sigma = 0
    use_fa = False
    
    print('Aligning images')
    crops, orig_images, quads = crop_faces_from_image(image_size, frame, scale, center_sigma=center_sigma, xy_sigma=xy_sigma, use_fa=use_fa)
    
    inv_transforms = [
        calc_alignment_coefficients(quad + 0.5, [[0, 0], [0, image_size], [image_size, image_size], [image_size, 0]])
        for quad in quads
    ]
    
    return crops, orig_images, quads, inv_transforms

def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())



def numpy_to_pil(images):
    """
    Convert a numpy image or a batch of images to a PIL image.
    """
    if images.ndim == 3:
        images = images[None, ...]
    images = (images * 255).round().astype("uint8")
    pil_images = [Image.fromarray(image) for image in images]

    return pil_images


def load_model_from_config(config, ckpt, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print("missing keys:")
        print(m)
    if len(u) > 0 and verbose:
        print("unexpected keys:")
        print(u)

    model.cuda()
    model.eval()
    return model


def put_watermark(img, wm_encoder=None):
    if wm_encoder is not None:
        img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        img = wm_encoder.encode(img, 'dwtDct')
        img = Image.fromarray(img[:, :, ::-1])
    return img


def load_replacement(x):
    try:
        hwc = x.shape
        y = Image.open("assets/rick.jpeg").convert("RGB").resize((hwc[1], hwc[0]))
        y = (np.array(y)/255.0).astype(x.dtype)
        assert y.shape == x.shape
        return y
    except Exception:
        return x


def check_safety(x_image):
    safety_checker_input = safety_feature_extractor(numpy_to_pil(x_image), return_tensors="pt")
    x_checked_image, has_nsfw_concept = safety_checker(images=x_image, clip_input=safety_checker_input.pixel_values)
    assert x_checked_image.shape[0] == len(has_nsfw_concept)
    for i in range(len(has_nsfw_concept)):
        if has_nsfw_concept[i]:
            x_checked_image[i] = load_replacement(x_checked_image[i])
    return x_checked_image, has_nsfw_concept


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--prompt",
        type=str,
        nargs="?",
        default="a photograph of an astronaut riding a horse",
        help="the prompt to render"
    )
    parser.add_argument(
        "--outdir",
        type=str,
        nargs="?",
        help="dir to write results to",
        default="results_video/debug"
    )
    parser.add_argument(
        "--Base_dir",
        type=str,
        nargs="?",
        help="dir to write cropped_images",
        default="results_video"
    )
    parser.add_argument(
        "--skip_grid",
        action='store_true',
        help="do not save a grid, only individual samples. Helpful when evaluating lots of samples",
    )
    parser.add_argument(
        "--skip_save",
        action='store_true',
        help="do not save individual samples. For speed measurements.",
    )
    parser.add_argument(
        "--ddim_steps",
        type=int,
        default=50,
        help="number of ddim sampling steps",
    )
    parser.add_argument(
        "--plms",
        action='store_true',
        help="use plms sampling",
    )
    parser.add_argument(
        "--laion400m",
        action='store_true',
        help="uses the LAION400M model",
    )
    parser.add_argument(
        "--fixed_code",
        action='store_true',
        help="if enabled, uses the same starting code across samples ",
        default=False
    )
    parser.add_argument(
        "--Start_from_target",
        action='store_true',
        help="if enabled, uses the noised target image as the starting ",
    )
    parser.add_argument(
        "--only_target_crop",
        action='store_true',
        help="if enabled, uses the noised target image as the starting ",
        default=True
    )
    parser.add_argument(
        "--target_start_noise_t",
        type=int,
        default=1000,
        help="target_start_noise_t",
    )
    parser.add_argument(
        "--ddim_eta",
        type=float,
        default=0.0,
        help="ddim eta (eta=0.0 corresponds to deterministic sampling",
    )
    parser.add_argument(
        "--n_iter",
        type=int,
        default=2,
        help="sample this often",
    )
    parser.add_argument(
        "--H",
        type=int,
        default=512,
        help="image height, in pixel space",
    )
    parser.add_argument(
        "--W",
        type=int,
        default=512,
        help="image width, in pixel space",
    )
    parser.add_argument(
        "--C",
        type=int,
        default=4,
        help="latent channels",
    )
    parser.add_argument(
        "--f",
        type=int,
        default=8,
        help="downsampling factor",
    )
    parser.add_argument(
        "--n_samples",
        type=int,
        default=12,
        help="how many samples to produce for each given prompt. A.k.a. batch size",
    )
    parser.add_argument(
        "--n_rows",
        type=int,
        default=0,
        help="rows in the grid (default: n_samples)",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=5,
        help="unconditional guidance scale: eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))",
    )
    parser.add_argument(
        "--target_folder",
        type=str,
        help="target_video",
        default="examples/faceswap/Andy2.mp4",
    )
    parser.add_argument(
        "--src_folder",
        type=str,
        help="src_image",
        default="examples/faceswap/source.jpg"
    )
    parser.add_argument(
        "--src_image_mask",
        type=str,
        help="src_image_mask",
    )
    parser.add_argument(
        "--from-file",
        type=str,
        help="if specified, load prompts from this file",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="configs/debug.yaml",
        help="path to config which constructs model",
    )
    parser.add_argument(
        "--ckpt",
        type=str,
        default="models/REFace/checkpoints/last.ckpt",
        help="path to checkpoint of model",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="the seed (for reproducible sampling)",
    )
    parser.add_argument(
        "--rank",
        type=int,
        default=0,
        help="the seed (for reproducible sampling)",
    )
    parser.add_argument(
        "--precision",
        type=str,
        help="evaluate at this precision",
        choices=["full", "autocast"],
        default="autocast"
    )
    
    parser.add_argument('--faceParser_name', default='default', type=str, help='face parser name, [ default | segnext] is currently supported.')
    parser.add_argument('--faceParsing_ckpt', type=str, default="Other_dependencies/face_parsing/79999_iter.pth")  
    parser.add_argument('--segnext_config', default='', type=str, help='Path to pre-trained SegNeXt faceParser configuration file, '
                                                                        'this option is valid when --faceParsing_ckpt=segenext')
            
    parser.add_argument('--save_vis', action='store_true')
    parser.add_argument('--seg12',default=True, action='store_true')
    
    opt = parser.parse_args()
    print(opt)
    # breakpoint()
    if opt.laion400m:
        print("Falling back to LAION 400M model...")
        opt.config = "configs/latent-diffusion/txt2img-1p4B-eval.yaml"
        opt.ckpt = "models/ldm/text2img-large/model.ckpt"
        opt.outdir = "outputs/txt2img-samples-laion400m"

    seed_everything(opt.seed)

    config = OmegaConf.load(f"{opt.config}")
    model = load_model_from_config(config, f"{opt.ckpt}")

    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    
    model = model.to(device)

    if opt.plms:
        sampler = PLMSSampler(model)
    else:
        sampler = DDIMSampler(model)


    os.makedirs(opt.outdir, exist_ok=True)
    outpath = opt.outdir
    Base_path=opt.Base_dir

    batch_size = opt.n_samples
    n_rows = opt.n_rows if opt.n_rows > 0 else batch_size
    if not opt.from_file:
        prompt = opt.prompt
        assert prompt is not None
        data = [batch_size * [prompt]]

    else:
        print(f"reading prompts from {opt.from_file}")
        with open(opt.from_file, "r") as f:
            data = f.read().splitlines()
            data = list(chunk(data, batch_size))

    # sample_path = os.path.join(outpath, "samples")
    result_path = os.path.join(outpath, "results")
    model_out_path = os.path.join(outpath, "model_outputs")
    
    
    
    target_path=opt.target_folder
    target_cropped_path=os.path.join(Base_path, "target_cropped")
    target_mask_path=os.path.join(Base_path,"mask_frames")
    source_cropped_path=os.path.join(Base_path, "source_cropped")
    source_mask_path=os.path.join(Base_path, "source_mask")
    
    # os.makedirs(sample_path, exist_ok=True)
    os.makedirs(result_path, exist_ok=True)
    os.makedirs(target_cropped_path, exist_ok=True)
    os.makedirs(target_mask_path, exist_ok=True)
    os.makedirs(model_out_path, exist_ok=True)
    os.makedirs(source_cropped_path, exist_ok=True)
    os.makedirs(source_mask_path, exist_ok=True)
    
    

    temp_results_dir = os.path.join(Base_path, 'temp_results')
    os.makedirs(temp_results_dir, exist_ok=True)
    
    faceParsing_model = init_faceParsing_pretrained_model(opt.faceParser_name, opt.faceParsing_ckpt, opt.segnext_config)
    
    
    
    
    # src_image=cv2.imread(opt.src_image)
    
    # base_count = len(os.listdir(sample_path))
    # grid_count = len(os.listdir(outpath)) - 1
    
    # get count of mask_frames_path
    # base_count = len(os.listdir(target_frames_path))
    # mask_count= len(os.listdir(mask_frames_path))
    
    
    # inv_transforms_all = []
    # for images in target_path
    
    reset_cnt=0
    for i,im in enumerate(os.listdir(target_path)):
        im_name=os.path.basename(im)
        i=i-reset_cnt

        try:
            
            crops, orig_images, quads, inv_transforms = crop_and_align_face([os.path.join(target_path, im)])
            crops = [crop.convert("RGB") for crop in crops]
            T = crops[0]
            # inv_transforms_all.append(inv_transforms[0])
            
            pil_im = T.resize((1024,1024), Image.BILINEAR)
            mask = faceParsing_demo(faceParsing_model, pil_im, convert_to_seg12=opt.seg12, model_name=opt.faceParser_name)
            Image.fromarray(mask).save(os.path.join(target_mask_path, f'{str(i)}.png'))
            # save T
            T.save(os.path.join(target_cropped_path, f'{str(i)}.png'))
        except:
            print(f"Error in {im}")
            reset_cnt+=1
            pass
        
        
    reset_cnt=0
    for i,im in enumerate(os.listdir(opt.src_folder)):
        im_name=os.path.basename(im)
        i=i-reset_cnt

        try:
            
            crops, orig_images, quads, inv_transforms = crop_and_align_face([os.path.join(opt.src_folder, im)])
            crops = [crop.convert("RGB") for crop in crops]
            T = crops[0]
            # inv_transforms_all.append(inv_transforms[0])
            
            pil_im = T.resize((1024,1024), Image.BILINEAR)
            mask = faceParsing_demo(faceParsing_model, pil_im, convert_to_seg12=opt.seg12, model_name=opt.faceParser_name)
            Image.fromarray(mask).save(os.path.join(source_mask_path , f'{str(i)}.png'))
            # save T
            T.save(os.path.join(source_cropped_path, f'{str(i)}.png'))
        except:
            print(f"Error in {im}")
            # if error do not skip i
            reset_cnt+=1
            pass
   
    
    # crops, orig_images, quads, inv_transforms = crop_and_align_face([opt.src_image])
    # crops = [crop.convert("RGB") for crop in crops]
    # T = crops[0]
    # src_image_new=os.path.join(temp_results_dir, src_name+'.png')
    # T.save(src_image_new)
    # pil_im = Image.open(src_image_new).convert("RGB").resize((1024,1024), Image.BILINEAR)
    # mask = faceParsing_demo(faceParsing_model, pil_im, convert_to_seg12=opt.seg12, model_name=opt.faceParser_name)
    # Image.fromarray(mask).save(os.path.join(temp_results_dir, os.path.basename(opt.src_image)))
    
    del faceParsing_model
    
    conf_file=OmegaConf.load(opt.config)
    ################### Get reference
    for i,im in enumerate(os.listdir(source_cropped_path)):
        result_img_path=os.path.join(result_path, str(i))
        grid_path = os.path.join(outpath, "grid",str(i))
        sample_path = os.path.join(outpath, str(i))
        os.makedirs(grid_path, exist_ok=True)
        os.makedirs(sample_path, exist_ok=True)
        os.makedirs(result_img_path, exist_ok=True)
        
        im_name=os.path.basename(im)
        trans=A.Compose([
                A.Resize(height=224,width=224)])
        ref_img_path = os.path.join(source_cropped_path, im)
        img_p_np=cv2.imread(ref_img_path)
        # ref_img = Image.open(ref_img_path).convert('RGB').resize((224,224))
        ref_img = cv2.cvtColor(img_p_np, cv2.COLOR_BGR2RGB)
        # ref_img= cv2.resize(ref_img, (224, 224))
        
        ref_mask_path = os.path.join(source_mask_path, im)
        ref_mask_img = Image.open(ref_mask_path).convert('L')
        ref_mask_img = np.array(ref_mask_img)  # Convert the label to a NumPy array if it's not already

        # Create a mask to preserve values in the 'preserve' list
  
        preserve=conf_file.data.params.test.params['preserve_mask_src_FFHQ']
        print(preserve)
        ref_mask= np.isin(ref_mask_img, preserve)

        # Create a converted_mask where preserved values are set to 255
        ref_converted_mask = np.zeros_like(ref_mask_img)
        ref_converted_mask[ref_mask] = 255
        ref_converted_mask=Image.fromarray(ref_converted_mask).convert('L')
        # convert to PIL image
        reference_mask_tensor=get_tensor(normalize=False, toTensor=True)(ref_converted_mask)
        mask_ref=transforms.Resize((224,224))(reference_mask_tensor)
        ref_img=trans(image=ref_img)
        ref_img=Image.fromarray(ref_img["image"])
        ref_img=get_tensor_clip()(ref_img)
        ref_img=ref_img*mask_ref
        ref_image_tensor = ref_img.to(device,non_blocking=True).to(torch.float16).unsqueeze(0)
        
        
        # ref_mask_img=Image.fromarray(ref_img).convert('L')
        # ref_mask_img_r = ref_converted_mask.resize(img_p_np.shape[1::-1], Image.NEAREST)
        # ref_mask_img_r = np.array(ref_mask_img_r)
        # ref_img[ref_mask_img_r==0]=0
        
        # ref_img=trans(image=ref_img)
        # ref_img=Image.fromarray(ref_img["image"])
        # ref_img=get_tensor_clip()(ref_img)
        
        # ref_image_tensor = ref_img.to(device,non_blocking=True).to(torch.float16).unsqueeze(0)
        ########################
        

        # test_dataset=COCOImageDataset(test_bench_dir='test_bench') 
        #read config file :configs/v2.yaml
        
        # breakpoint()
        test_args=conf_file.data.params.test.params
        
        
        
        
        test_dataset=VideoDataset(data_path=target_cropped_path,mask_path=target_mask_path,**test_args)
        test_dataloader= torch.utils.data.DataLoader(test_dataset, 
                                            batch_size=batch_size, 
                                            num_workers=4, 
                                            pin_memory=True, 
                                            shuffle=False,#sampler=train_sampler, 
                                            drop_last=False)





        start_code = None
        if opt.fixed_code:
            print("Using fixed code.......")
            start_code = torch.randn([ opt.C, opt.H // opt.f, opt.W // opt.f], device=device)
            # extend the start code to batch size
            start_code = start_code.unsqueeze(0).repeat(batch_size, 1, 1, 1)

    
        start_code = None
        if opt.fixed_code:
            start_code = torch.randn([opt.n_samples, opt.C, opt.H // opt.f, opt.W // opt.f], device=device)

    
        use_prior=False
        
        precision_scope = autocast if opt.precision=="autocast" else nullcontext
        sample=0
        with torch.no_grad():
            with precision_scope("cuda"):
                with model.ema_scope():
                    all_samples = list()
                    for test_batch,prior, test_model_kwargs,segment_id_batch in test_dataloader:
                        sample+=opt.n_samples
                        # if sample<980:
                        #     continue
                        if opt.Start_from_target:
                            print("Starting from target....")
                            x=test_batch
                            x=x.to(device)
                            encoder_posterior = model.encode_first_stage(x)
                            z = model.get_first_stage_encoding(encoder_posterior)
                            t=int(opt.target_start_noise_t)
                            # t = torch.ones((x.shape[0],), device=device).long()*t
                            t = torch.randint(t-1, t, (x.shape[0],), device=device).long()
                        
                            if use_prior:
                                prior=prior.to(device)
                                encoder_posterior_2=model.encode_first_stage(prior)
                                z2 = model.get_first_stage_encoding(encoder_posterior_2)
                                noise = torch.randn_like(z2)
                                x_noisy = model.q_sample(x_start=z2, t=t, noise=noise)
                                start_code = x_noisy
                                # print('start from target')
                            else:
                                noise = torch.randn_like(z)
                                x_noisy = model.q_sample(x_start=z, t=t, noise=noise)
                                start_code = x_noisy
                            # print('start from target')
                            
                        test_model_kwargs={n:test_model_kwargs[n].to(device,non_blocking=True) for n in test_model_kwargs }
                        uc = None
                        if opt.scale != 1.0:
                            uc = model.learnable_vector.repeat(test_batch.shape[0],1,1)
                            if model.stack_feat:
                                uc2=model.other_learnable_vector.repeat(test_batch.shape[0],1,1)
                                uc=torch.cat([uc,uc2],dim=-1)
                        
                        # c = model.get_learned_conditioning(test_model_kwargs['ref_imgs'].squeeze(1).to(torch.float16))
                        landmarks=model.get_landmarks(test_batch) if model.Landmark_cond else None
                        ref_imgs=ref_image_tensor
                        # stack it ref_imgs to the shape of test_batch
                        ref_imgs=ref_imgs.repeat(test_batch.shape[0],1,1,1)
                        
                        c=model.conditioning_with_feat(ref_imgs.squeeze(1).to(torch.float32),landmarks=landmarks,tar=test_batch.to(device).to(torch.float32)).float()
                        if (model.land_mark_id_seperate_layers or model.sep_head_att) and opt.scale != 1.0:
                
                            # concat c, landmarks
                            landmarks=landmarks.unsqueeze(1) if len(landmarks.shape)!=3 else landmarks
                            uc=torch.cat([uc,landmarks],dim=-1)
                        
                        
                        if c.shape[-1]==1024:
                            c = model.proj_out(c)
                        if len(c.shape)==2:
                            c = c.unsqueeze(1)
                        inpaint_image=test_model_kwargs['inpaint_image']
                        inpaint_mask=test_model_kwargs['inpaint_mask']
                        z_inpaint = model.encode_first_stage(test_model_kwargs['inpaint_image'])
                        z_inpaint = model.get_first_stage_encoding(z_inpaint).detach()
                        test_model_kwargs['inpaint_image']=z_inpaint
                        test_model_kwargs['inpaint_mask']=Resize([z_inpaint.shape[-1],z_inpaint.shape[-1]])(test_model_kwargs['inpaint_mask'])

                        shape = [opt.C, opt.H // opt.f, opt.W // opt.f]
                        # breakpoint()
                        samples_ddim, intermediates = sampler.sample(S=opt.ddim_steps,
                                                            conditioning=c,
                                                            batch_size=test_batch.shape[0],
                                                            shape=shape,
                                                            verbose=False,
                                                            unconditional_guidance_scale=opt.scale,
                                                            unconditional_conditioning=uc,
                                                            eta=opt.ddim_eta,
                                                            x_T=start_code,
                                                            test_model_kwargs=test_model_kwargs,src_im=ref_imgs.squeeze(1).to(torch.float32),tar=test_batch.to(device))
                        # breakpoint()
                        # save intermediates
                        other_pred_x_0=intermediates['pred_x0']
                        for i in range(len(other_pred_x_0)):
                            pred_x0=model.decode_first_stage(other_pred_x_0[i])
                            x_sample = torch.clamp((pred_x0 + 1.0) / 2.0, min=0.0, max=1.0)
                            x_sample = x_sample.cpu().permute(0, 2, 3, 1).numpy()
                            x_sample=torch.from_numpy(x_sample).permute(0, 3, 1, 2)
                            x_sample=x_sample[0]
                            x_sample=255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                            x_sample=Image.fromarray(x_sample.astype(np.uint8))
                            x_sample.save(os.path.join(model_out_path,"_intermediate_"+str(i)+".png"))
                        x_samples_ddim = model.decode_first_stage(samples_ddim)
                        x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)
                        x_samples_ddim = x_samples_ddim.cpu().permute(0, 2, 3, 1).numpy()

                        x_checked_image=x_samples_ddim
                        x_checked_image_torch = torch.from_numpy(x_checked_image).permute(0, 3, 1, 2)

                        def un_norm(x):
                            return (x+1.0)/2.0
                        def un_norm_clip(x1):
                            x = x1*1.0 # to avoid changing the original tensor or clone() can be used
                            reduce=False
                            if len(x.shape)==3:
                                x = x.unsqueeze(0)
                                reduce=True
                            x[:,0,:,:] = x[:,0,:,:] * 0.26862954 + 0.48145466
                            x[:,1,:,:] = x[:,1,:,:] * 0.26130258 + 0.4578275
                            x[:,2,:,:] = x[:,2,:,:] * 0.27577711 + 0.40821073
                            
                            if reduce:
                                x = x.squeeze(0)
                            return x

                        if not opt.skip_save:
                            for i,x_sample in enumerate(x_checked_image_torch):
                                

                                all_img=[]
                                all_img.append(un_norm(test_batch[i]).cpu())
                                all_img.append(un_norm(inpaint_image[i]).cpu())
                                ref_img=ref_imgs.squeeze(1)
                                ref_img=Resize([512,512])(ref_img)
                                all_img.append(un_norm_clip(ref_img[i]).cpu())
                                all_img.append(x_sample)
                                grid = torch.stack(all_img, 0)
                                grid = make_grid(grid)
                                grid = 255. * rearrange(grid, 'c h w -> h w c').cpu().numpy()
                                img = Image.fromarray(grid.astype(np.uint8))
                                img.save(os.path.join(grid_path, 'grid-'+segment_id_batch[i]+'.png'))
                                



                                x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                                img = Image.fromarray(x_sample.astype(np.uint8))
                                img.save(os.path.join(result_img_path, segment_id_batch[i]+".png"))
                                
                                mask_save=255.*rearrange(un_norm(inpaint_mask[i]).cpu(), 'c h w -> h w c').numpy()
                                mask_save= cv2.cvtColor(mask_save,cv2.COLOR_GRAY2RGB)
                                mask_save = Image.fromarray(mask_save.astype(np.uint8))
                                mask_save.save(os.path.join(sample_path, segment_id_batch[i]+"_mask.png"))
                                GT_img=255.*rearrange(all_img[0], 'c h w -> h w c').numpy()
                                GT_img = Image.fromarray(GT_img.astype(np.uint8))
                                GT_img.save(os.path.join(sample_path, segment_id_batch[i]+"_GT.png"))
                                inpaint_img=255.*rearrange(all_img[1], 'c h w -> h w c').numpy()
                                inpaint_img = Image.fromarray(inpaint_img.astype(np.uint8))
                                inpaint_img.save(os.path.join(sample_path, segment_id_batch[i]+"_inpaint.png"))
                                ref_img=255.*rearrange(all_img[2], 'c h w -> h w c').numpy()
                                ref_img = Image.fromarray(ref_img.astype(np.uint8))
                                ref_img.save(os.path.join(sample_path, segment_id_batch[i]+"_ref.png"))
                                # base_count += 1



                        if not opt.skip_grid:
                            all_samples.append(x_checked_image_torch)


    print(f"Your samples are ready and waiting for you here: \n{outpath} \n"
          f" \nEnjoy.")

 


if __name__ == "__main__":
    main()

