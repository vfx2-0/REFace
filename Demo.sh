@echo off

REM Set variables
set name=One_output
set Results_dir=examples/FaceSwap/%name%/results
set Base_dir=examples/FaceSwap/%name%/Outs
set Results_out=examples/FaceSwap/%name%/results/results
set device=0

set CONFIG=models/REFace/configs/project_ffhq.yaml
set CKPT=models/REFace/checkpoints/saved.ckpt

REM Change this
set target_path=examples/FaceSwap/One_target
set source_path=examples/FaceSwap/One_source

REM Run inference
python scripts\one_inference.py ^
    --outdir "%Results_dir%" ^
    --target_folder "%target_path%" ^
    --config "%CONFIG%" ^
    --ckpt "%CKPT%" ^
    --src_folder "%source_path%" ^
    --Base_dir "%Base_dir%" ^
    --n_samples 1 ^
    --scale 3.5 ^
    --ddim_steps 50
