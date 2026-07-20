# LitE-SQL
Official code for "**LitE-SQL: A Lightweight and Efficient Text-to-SQL Framework with Vector-based Schema Linking and Execution-Guided Self-Correction**"
![Method-Inference-final](https://github.com/user-attachments/assets/16d9c03d-926d-40d3-aa98-0e7bb4a337f0)

🔥 **Our paper has been accepted to Findings of EACL 2026.**

## :bookmark_tabs: Table of Contents

- :hammer_and_wrench: [Getting Started](#hammer_and_wrench-getting-started)
- :rocket: [Running LitE-SQL](#rocket-running-tinythinker)
  - :mag: [Schema Retriever](#mag-schema-retriever)
    - :memo: [Pre-process Dataset](#memo-pre-process-dataset)
    - :gear: [Fine-tune Embedding Model](#gear-fine-tune-embedding-model)
    - :hourglass_flowing_sand: [Run Schema Retriever](#hourglass_flowing_sand-run-schema-retriever)
  - :dart: [SQL Generator](#dart-SQL-generator)
    - :memo: [Prepare Training Dataset](#memo-prepare-training-dataset)
    - :gear: [Train SQL Generator](#gear-train-SQL-generator)
    - :hourglass_flowing_sand: [Inference](#hourglass_flowing_sand-inference)
- :page_facing_up: [License](#page_facing_up-license)

## :hammer_and_wrench: Getting Started

To set up the environment, use the following commands:

```
git clone https://github.com/shengminp/LitE-SQL.git
cd LitE-SQL
conda env create -f environment.yml
```

After installation, your project directory structure should look like this:
    
    .
    ├── datasets
    ├── schema_retriever
    ├── sql_generator
    ├── environment.yml
    └── README.md

Specifically, after downloading the dataset, you should arrange the directory structure of the datasets as follows:

    .
    └── datasets
         ├── bird
         |    |── train
         |    |    ├── train_databases
         |    |    ├── train_tables.json
         |    |    ├── train.json
         |    |    └── train_gold.sql
         |    └── dev_20240627
         |         ├── dev_databases
         |         ├── dev.json
         |         ├── dev_tables.json
         |         ├── dev_tied_append.json
         |         └── dev.sql
         └── spider
              |── train
              |    ├── database
              |    ├── train_gold.sql
              |    ├── train_others.json
              |    ├── tables.json
              |    └── train_spider.sql
              |── dev
              |    ├── database
              |    ├── dev.json
              |    ├── tables.json
              |    └── dev_gold.sql
              └── test
                   ├── test_database
                   ├── test.json
                   ├── test_tables.json
                   └── test_gold.sql

## :rocket: Running LitE-SQL

### :mag: Schema Retriever
The directory structure of schema retriever is as follow:

    .
    └──schema_retriever
        ├── data
        │    └── preprocessing.py
        ├── language_model
        │    |── language_model.py
        │    └── saved_model
        ├── utils
        │    ├── configs.py
        │    ├── dataset_util.py
        │    ├── db_utils.py
        |    └── utils.py        
        └── scripts
             ├── fine-tune.py
             └── retrieve.py

#### :memo: Pre-process Dataset
Please refer to `./schema_retriever/data/README.md`  
NOTE: `./schema_retriever/data/preprocessing.py` must be executed before following steps.

#### :gear: Fine-tune Embedding Model

After this fine-tuning step, the embedding model would be saved on `$ft_path/$model_name`. Run the following command:
```
python schema_retriever/scripts/fine-tune.py \
    --ft_path $ft_path \
    --model_name $model_name \
    --epoch $epoch \
    --batch_size $batch_size \
    --n_limit $n_limit \
    --LM_MODEL $LM_MODEL \
    --DATA_PATH $DATA_PATH \
```
- **$ft_path:** Path of embedding model to be saved (`schema_retriever/language_model/saved_model`)
- **$model_name:** Name of embedding model (`fine-tuned-embedding-model`)
- **$LM_MODEL:** Embedding model from Huggingface (`intfloat/multilingual-e5-large`)
- **$DATA_PATH:** Path for training dataset (`schema_retriever/data/fine-tuning_samples_from_BIRD_augmented_version.json`)
  
#### :hourglass_flowing_sand: Run Schema Retriever

This Schema Retriever code is for BIRD. After running this retrieval step, retrieved schema information would be saved on `sql_generator/dev_20240627/retrieved/BIRD-dev-more-schema.json`. Run the following command:
```
python schema_retriever/scripts/retrieve.py \
    --root_path $root_path \
    --db_schema_info_path $db_schema_info_path \
    --data_path $data_path \
    --database_dir_path $database_dir_path \
    --SL_K $K
```
- **$root_path:** Path of dataset root dir (`datasets/bird/dev_20240627`)
- **$db_schema_info_path:** Path of schema info (`dev_tables.json`)
- **$data_path:** Path of dataset (`dev.json`)
- **$database_dir_path:** Path of database (`dev_databases`)
- **$SL_K:** The number of retrieved columns (`25`)
---------------------------------------------------------------------
### :dart: SQL Generator
The directory structure of SQL generator is as follow:

    .
    └──sql_generator
        ├── datasets
        │    └── Prepare.ipynb
        ├── models
        ├── results
        └── scripts
             ├── utils
             │    ├── spider_tool
             |    |     ├── __init__.py
             |    |     ├── evaluation.py
             |    |     ├── exec_eval.py
             |    |     ├── parse.py
             |    |     └── process_sql.py
             │    ├── __init__.py
             │    ├── config.py
             │    ├── data.py
             │    ├── metric.py
             |    └── trainer.py
             ├── rft.py
             ├── sft.py
             └── generate.py


#### :memo: Prepare Training Dataset
1. Run ```./datasets/Prepare.ipynb``` to prepare dataset for suervised fine-tuning.
2. The dataset directory should look like this:
```
.
└── datasets
     ├── bird
     |    |── train
     |    |    ├── sft
     |    |    |    └── sft_train.json
     |    |    ├── rft
     |    |    ├── BIRD-train-more-schema.json
     |    |    ...
     |    └── dev_20240627
     |         ├── retrieved
     |         |    ├── BIRD-dev-more-schema.json
     |         |    ...
     |         ...
     └── spider
          |── train
          |    ├── sft
          |    |    └── sft_train.json
          |    ├── rft
          |    ├── SPIDER-train-more-schema.json
          |    ...
          |── dev
          |    ├── retrieved
          |    |    ├── SPIDER-dev-more-schema.json
          |    |    ...
          |    ...
          └── test
               ├── retrieved
               |    ├── SPIDER-test-more-schema.json
               |    ...
               ...
```

#### :gear: Train SQL Generator
**Phase-1: Supervised Fine-tuning**

In this phase, a Qwen2.5-Coder model is fine-tuned. Run the following command:
```
python sft.py \
    --base_model $model_name \
    --data_name $data_name \
    --training_phase sft \
```
- **$model_name:** Model name from Huggingface ```(Qwen/Qwen2.5-Coder-1.5B-Instruct, Qwen/Qwen2.5-Coder-3B-Instruct, Qwen/Qwen2.5-Coder-7B-Instruct)```.
- **$data_name:** Dataset name ```(bird, spider)```.

**Phase-2: Reinforcement Fine-tuning**

In this phase, we fine tune the model with self-generated data. Run the following command:
```
python rft.py \
    --base_model $base_model \
    --tune_model $checkpoint \
    --ref_model $checkpoint \
    --data_name $data_name \
    --training_phase rft \
    --rft_iter $rft_iter \
```
- **$base_model:** Model name from Huggingface ```(Qwen/Qwen2.5-Coder-1.5B-Instruct, Qwen/Qwen2.5-Coder-3B-Instruct, Qwen/Qwen2.5-Coder-7B-Instruct)```.
- **$tune_model:** Path of checkpoint from sft pahse.
- **$ref_model:** Same as $tune_model.
- **$data_name:** Dataset name ```(bird, spider)```.

#### :hourglass_flowing_sand: Inference
Use the following command to do inferences after sft phase:
```
python generate.py \
    --base_model $base_model \
    --data_name $data_name \
    --training_phase sft \
    --checkpoint_dir $checkpoint_path\
    --schema_file $schema_file_path\
    --generation_mode $generation_mode \
    --generation_type $generation_type \
    --generation_file $generation_file
```
- **$base_name:** Model name ```(Qwen/Qwen2.5-Coder-1.5B-Instruct, Qwen/Qwen2.5-Coder-3B-Instruct, Qwen/Qwen2.5-Coder-7B-Instruct)```.
- **$data_name:** Dataset name ```(bird, spider)```.
- **$checkpoint_dir:** Path of checkpoint.
- **$schema_file:** Path of schema file
- **$generation_mode:** Generation mode ```(generate, revise)```.
- **$generation_type:** Generation type ```(greedy, random)```.
- **$generation_file:** Generation file ```(test, augment)```.

Use the following command to generate data for rft phase:
```
python generate.py \
    --base_model $base_model \
    --data_name $data_name \
    --training_phase sft \
    --checkpoint_dir $checkpoint_path\
    --schema_file $schema_file_path\
    --generation_mode $generation_mode \
    --generation_type $generation_type \
    --generation_file $generation_file \
    --augment_times $augment_times \
    --generate_times $generate_times \
    --rft_iter 1 \
```
- **$augment_times:** The times to augment the training data.
- **$generate_times:** The times to generate new data.

Use the following command to do inferences after rft phase:
```
python generate.py \
    --base_model $base_model \
    --data_name $data_name \
    --training_phase rft \
    --checkpoint_dir $checkpoint_path\
    --schema_file $schema_file_path\
    --generation_mode $generation_mode \
    --generation_type $generation_type \
    --generation_file $generation_file \
    --revise_times $revise_times \
    --rft_iter 1 \
```
- **$revise_times:** The times to revise the generated SQL.

## :page_facing_up: License

This project is licensed under the [MIT](LICENSE) © Shengmin Piao & Jieun Lee

---

## 🇻🇳 Hướng Dẫn Chạy Đánh Giá (Vietnamese Running Guide)

LitE-SQL hỗ trợ đánh giá hiệu năng Text-to-SQL trên ba bộ dữ liệu benchmark phổ biến: **Spider**, **BIRD**, và **FloodSQL-Bench**. Dưới đây là hướng dẫn chi tiết cách chạy từng phần bằng tiếng Việt.

### 1. Đánh Giá Trên Bộ Dữ Liệu Spider
Để chạy đánh giá baseline trên toàn bộ tập dữ liệu Spider dev set:
1. Đảm bảo bạn đã tải và giải nén các cơ sở dữ liệu (databases) SQLite của Spider vào thư mục tương ứng.
2. Chạy lệnh:
   ```bash
   python run_spider_evaluation.py
   ```
*Kết quả dự đoán sẽ được lưu tại `results/Spider_Qwen3.6-27B-GGUF_results.json`.*

### 2. Đánh Giá Trên Bộ Dữ Liệu BIRD
Để chạy đánh giá baseline trên tập dữ liệu BIRD:
1. Thiết lập thư mục databases BIRD và tệp dev.json.
2. Chạy lệnh:
   ```bash
   python run_bird_evaluation.py
   ```
*Kết quả dự đoán sẽ được lưu tại `results/BIRD_Qwen3.6-27B-GGUF_results.json`.*

### 3. Đánh Giá Trên Bộ Dữ Liệu FloodSQL-Bench
Để chạy đánh giá trên bộ dữ liệu FloodSQL-Bench sử dụng framework LitE-SQL:
1. Đảm bảo đường dẫn dữ liệu FloodSQL-Bench trỏ tới thư mục dữ liệu tương ứng (mặc định: `D:/Projects/FloodSQL-Bench-main/data`).
2. Chạy lệnh:
   ```bash
   python run_flood_evaluation.py
   ```
*Kết quả dự đoán sẽ được lưu dưới dạng JSON tại `results/FloodSQL_LitE-SQL_results.json`.*

### 📊 Kết Quả Đánh Giá (Evaluation Results)
Dưới đây là kết quả độ chính xác thực thi (Execution Accuracy - EX) của mô hình `Qwen3.6-27B-GGUF` chạy trên framework LitE-SQL:

| Bộ dữ liệu (Benchmark) | Số lượng câu hỏi (Queries) | Độ chính xác thực thi (EX Accuracy) | Ghi chú |
| :--- | :---: | :---: | :--- |
| **Spider** (Dev set) | 1034 | **79.11%** | Chạy baseline của LitE-SQL |
| **BIRD** (Dev set) | 1534 | **51.96%** | Chạy baseline của LitE-SQL |
| **FloodSQL-Bench** | 443 | **40.86%** | Chạy trên môi trường DuckDB Spatial |

---
*Lưu ý: Tất cả các tập lệnh trên đều sử dụng mô hình `Qwen3.6-27B-GGUF` thông qua API cục bộ, có hỗ trợ cơ chế tự sửa lỗi (execution-guided self-correction) qua phản hồi lỗi của hệ quản trị cơ sở dữ liệu.*


