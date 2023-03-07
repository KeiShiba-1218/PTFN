GPU_ID=5
CONFIG="experiments/ptfn_simplegate2/config_ptfn3.json"
noise_levels="50"
CUDA_VISIBLE_DEVICES=$GPU_ID python -m eval_codes.generate_images_davis -nl $noise_levels -c $CONFIG #--not_generae_inter_img
python -m eval_codes.evaluation -nl $noise_levels -c $CONFIG
#CUDA_VISIBLE_DEVICES=$GPU_ID python -m eval_codes.generate_images_set8 -nl $noise_levels -c $CONFIG --not_generae_inter_img
#python -m eval_codes.evaluation -nl $noise_levels --set8 -c $CONFIG