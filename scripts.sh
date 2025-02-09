
# Trivia_QA
python generate_response.py --output_dir ../dataset/grade_school_math/data/train_response_temp=0.jsonl --model_name /home/jl_fs/meta-llama/Llama-3.1-8B-Instruct-official --dataset gsm8k_dataset --split train
python generate_response.py --output_dir ../dataset/grade_school_math/data/test_response_temp=0.jsonl --model_name /home/jl_fs/meta-llama/Llama-3.1-8B-Instruct-official --dataset gsm8k_dataset --split test
python uncertainty_sft.py --use_wandb --add_loss_con True --on_policy False --batch_size_testing 10 --test_original_model False --do_sample False --temperature 0.1 --use_peft --peft_method lora --model_name /home/jl_fs/meta-llama/Llama-3.1-8B-Instruct-official --output_dir checkpoints/trivia_0104_loss_con --dataset trivia_qa --batch_size_training=8 --val_batch_size=8 --generate=llm --lr=5e-5 --loss_type=sot --num_epochs=2
# Key parameters:
# --on_policy = True/False : if the model always generate new answers when training
# --test_original_model = True/False : If to test the performance of original Llama
# --dataset = trivia_qa / gsm8k_dataset