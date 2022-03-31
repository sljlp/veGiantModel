#! /bin/bash
# Runs the "345M" parameter model

DATA_PATH=/root/paddlejob/workspace/env_run/data  #<Specify path where >
CHECKPOINT_PATH=/tmp/checkpoint

export WORKER_0_HOST=127.0.0.1
export DMLC_NODE_HOST=127.0.0.1
export WORKER_0_PORT=6000
export NUM_WORKER=1
export WORKER_RANK=0
export GPU_PER_WORKER=2

export BYTEPS_WITH_UCX=0
export DMLC_ENABLE_UCX=0
export DMLC_ENABLE_RDMA=0

export DMLC_NUM_WORKER=$((GPU_PER_WORKER * NUM_WORKER))

MASTER_PORT=6002
MASTER_ADDR=$WORKER_0_HOST

GPUS_PER_NODE=$GPU_PER_WORKER

NNODES=$NUM_WORKER
NODE_RANK=$WORKER_RANK

WORLD_SIZE=$(($GPUS_PER_NODE*$NNODES))

base_dir=$(cd `dirname $0`; pwd)
echo base_dir $base_dir

DISTRIBUTED_ARGS="--nproc_per_node $GPUS_PER_NODE --nnodes $NNODES --node_rank $NODE_RANK --master_addr $MASTER_ADDR --master_port $MASTER_PORT"

ds_config='{
    "train_micro_batch_size_per_gpu":64,
    "train_batch_size" : 64,
    "gradient_accumulation_steps": 1,
    "steps_per_print": 1,
    "gradient_clipping": 1.0,
    "zero_optimization": {
      "stage": 0,
      "allgather_partitions": true,
      "allgather_bucket_size": 500000000,
      "overlap_comm": true,
      "reduce_scatter": true,
      "reduce_bucket_size": 500000000,
      "contiguous_gradients" : true,
      "cpu_offload": false
    },
    "fp16": {
      "enabled": true,
      "loss_scale": 0,
      "loss_scale_window": 1000,
      "hysteresis": 2,
      "min_loss_scale": 1
    },
    "wall_clock_breakdown": true
}'
CUDA_VISIBLE_DEVICES=0,1 \
python3 -m torch.distributed.launch $DISTRIBUTED_ARGS \
       --no_python --use_env python3 \
       ${base_dir}/pretrain_gpt2.py \
       --model-parallel-size 1 \
       --num-stages 2 \
       --num-layers 16 \
       --hidden-size 768 \
       --train-batch-size 4 \
       --gradient_accumulation_steps 2 \
       --num-attention-heads 12 \
       --batch-size 2 \
       --seq-length 1024 \
       --max-position-embeddings 1024 \
       --train-iters 500000 \
       --lr-decay-iters 450000 \
       --save $CHECKPOINT_PATH \
       --load $CHECKPOINT_PATH \
       --data-path $DATA_PATH/my-gpt2_text_document \
       --vocab-file $DATA_PATH/gpt2-vocab.json \
       --merge-file $DATA_PATH/gpt2-merges.txt \
       --data-impl mmap \
       --split 949,50,1 \
       --distributed-backend nccl \
       --lr 0.00025 \
       --lr-decay-style cosine \
       --min-lr 1.0e-5 \
       --weight-decay 1e-2 \
       --clip-grad 1.0 \
       --warmup .02 \
       --log-interval 1 \
       --save-interval 100000 \
       --vocab-size 145608 \
       --DDP-impl torch \
       --eod-mask-loss \
       --deepspeed-pipeline \
       --deepspeed \
       --config_param "$ds_config" \
       --fp16 \
       --partition_method "type:ParallelTransformerLayerPiped" \
       $@
set +x
