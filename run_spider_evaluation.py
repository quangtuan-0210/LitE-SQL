import os
import json
import sqlite3
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import chromadb
from openai import OpenAI, RateLimitError

# Khởi tạo các OpenAI Client để gọi API Embeddings và API Chat Qwen
embedding_client = OpenAI(
    base_url="https://embedding.openclassroomteam.com/v1",
    api_key="f0GVKMTEP4VBkjjAymxyYftvi6xX2mVz"
)

chat_client = OpenAI(
    base_url="https://appresearchpublic83.aiplatform.vcntt.tech/v1",
    api_key="sglang",
    timeout=30.0
)

# Hàm sinh vector nhúng (embeddings) sử dụng mô hình Jina với cơ chế tự động thử lại khi gặp lỗi rate limit
def get_embeddings(texts, model_name="jina-embeddings-v3"):
    batch_size = 16
    embeddings = []
    
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i+batch_size]
        attempts = 0
        while True:
            try:
                response = embedding_client.embeddings.create(
                    input=batch,
                    model=model_name
                )
                embeddings.extend([data.embedding for data in response.data])
                break
            except RateLimitError:
                attempts += 1
                sleep_time = (2 ** attempts) + random.random()
                print(f"[Embedding API] Rate limit reached. Sleeping {sleep_time:.2f}s...")
                time.sleep(sleep_time)
            except Exception as e:
                attempts += 1
                if attempts > 5:
                    raise e
                sleep_time = (2 ** attempts) + random.random()
                print(f"[Embedding API] Error: {e}. Retrying in {sleep_time:.2f}s...")
                time.sleep(sleep_time)
                
    return embeddings

# Khởi tạo ChromaDB Client chạy trong RAM (In-memory)
chroma_client = chromadb.Client()

# Bộ nhớ đệm (Cache) toàn cục lưu thông tin lược đồ database
db_cache = {}
cache_lock = Lock()

# Các khóa (Locks) đồng bộ hóa để ghi tệp và in nhật ký an toàn đa luồng
file_lock = Lock()
print_lock = Lock()

# Đường dẫn tệp Checkpoint để tự động khôi phục tiến trình khi bị ngắt quãng
checkpoint_path = "d:/Projects/LitE-SQL/datasets/spider/dev/evaluation_checkpoint.json"

def get_db_schema(db_path):
    """Extract schema, sample values, and foreign keys from SQLite db."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Get tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [row[0] for row in cursor.fetchall()]
    
    schema = {}
    foreign_keys = []
    
    for table in tables:
        # Get columns
        cursor.execute(f'PRAGMA table_info("{table}")')
        cols = cursor.fetchall()
        
        schema[table] = {}
        for col in cols:
            col_name = col[1]
            col_type = col[2]
            is_pk = bool(col[5])
            
            # Get sample values
            try:
                cursor.execute(f'SELECT "{col_name}" FROM "{table}" WHERE "{col_name}" IS NOT NULL AND "{col_name}" != "" LIMIT 3')
                vals = [str(r[0]) for r in cursor.fetchall()]
            except:
                vals = []
                
            schema[table][col_name] = {
                "type": col_type,
                "primary_key": is_pk,
                "values": vals
            }
            
        # Get foreign keys
        cursor.execute(f'PRAGMA foreign_key_list("{table}")')
        fks = cursor.fetchall()
        for fk in fks:
            target_table = fk[2]
            from_col = fk[3]
            to_col = fk[4]
            foreign_keys.append(f"{table}.{from_col} = {target_table}.{to_col}")
            
    conn.close()
    return schema, foreign_keys

def get_or_create_db_cache(db_id, db_path):
    """Ensure database schema is extracted and its columns are embedded/cached in ChromaDB."""
    with cache_lock:
        if db_id in db_cache:
            return db_cache[db_id]
            
        print(f"[Cache] Preparing vector database cache for {db_id}...")
        schema, foreign_keys = get_db_schema(db_path)
        
        # Create a unique collection in ChromaDB
        col_name = f"col_{db_id}_{random.randint(1, 1000000)}"
        collection = chroma_client.create_collection(name=col_name)
        
        documents = []
        metadatas = []
        ids = []
        
        idx = 0
        for table_name, cols in schema.items():
            for col_key, info in cols.items():
                doc_text = f"Table: {table_name}, Column: {col_key}, Type: {info['type']}, Values: {info['values']}"
                documents.append(doc_text)
                metadatas.append({"table": table_name, "column": col_key})
                ids.append(f"col_{idx}")
                idx += 1
                
        # Bulk embed and add to collection
        if documents:
            embeddings = get_embeddings(documents)
            collection.add(
                documents=documents,
                embeddings=embeddings,
                metadatas=metadatas,
                ids=ids
            )
            
        db_cache[db_id] = {
            "schema": schema,
            "foreign_keys": foreign_keys,
            "collection": collection,
            "doc_count": len(documents)
        }
        return db_cache[db_id]

def retrieve_schema(question, db_cache_info, top_k=15):
    """Query pre-indexed ChromaDB collection using the question's vector."""
    collection = db_cache_info["collection"]
    doc_count = db_cache_info["doc_count"]
    schema = db_cache_info["schema"]
    
    if doc_count == 0:
        return {}
        
    q_emb = get_embeddings([question])[0]
    results = collection.query(
        query_embeddings=[q_emb],
        n_results=min(top_k, doc_count)
    )
    
    retrieved_schema = {}
    for meta in results['metadatas'][0]:
        t = meta['table']
        c = meta['column']
        if t not in retrieved_schema:
            retrieved_schema[t] = {}
        retrieved_schema[t][c] = schema[t][c]
        
    return retrieved_schema

def build_prompt(question, retrieved_schema, foreign_keys, evidence=None):
    """Build prompt for LLM SQL Generation."""
    schema_info = ["\n### Database\n-- Tables and Columns"]
    for table_name, cols in retrieved_schema.items():
        for col_name, info in cols.items():
            column_data = {
                "type": info["type"].upper(),
                "primary_key": info["primary_key"],
            }
            if info["values"]:
                column_data["values"] = info["values"]
            schema_info.append(f"{table_name}.{col_name} = {json.dumps(column_data)}")
            
    schema_text = "\n".join(schema_info)
    parts = [f"### Question\n{question.strip()}", schema_text]
    
    if foreign_keys:
        fk_text = " | ".join(foreign_keys)
        parts.append(f"\n-- Foreign Keys\n{fk_text}")
        
    if evidence:
        parts.append(f"\n-- Evidence\n{evidence.strip()}")
        
    return "\n".join(parts)

def generate_sql(prompt):
    """Gửi yêu cầu tới mô hình Qwen-27B qua API để dịch câu hỏi sang truy vấn SQLite SQL (không suy nghĩ)."""
    attempts = 0
    while True:
        try:
            response = chat_client.chat.completions.create(
                model="Qwen3.6-27B-GGUF",
                messages=[
                    {"role": "system", "content": "You are a translation assistant from text to SQL. Given a question, database schema, and foreign keys, generate the correct SQLite query. Return ONLY the SQL query wrapped in a ```sql ... ``` block."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.0,
                max_tokens=150,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}}
            )
            content = response.choices[0].message.content
            sql_match = re.search(r"```sql\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
            if sql_match:
                return sql_match.group(1).strip()
            return content.strip()
        except RateLimitError:
            attempts += 1
            sleep_time = (2 ** attempts) + random.random()
            print(f"[LLM API] Rate limit reached. Sleeping {sleep_time:.2f}s...")
            time.sleep(sleep_time)
        except Exception as e:
            attempts += 1
            if attempts > 5:
                print(f"[LLM API] Failed after 5 attempts: {e}")
                return ""
            sleep_time = (2 ** attempts) + random.random()
            print(f"[LLM API] Error: {e}. Retrying in {sleep_time:.2f}s...")
            time.sleep(sleep_time)

def execute_sql(db_path, sql):
    """Thực thi câu lệnh SQL trên cơ sở dữ liệu SQLite và trả về kết quả hoặc thông báo lỗi."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        res = cursor.fetchall()
        conn.close()
        return {"success": True, "result": res}
    except Exception as e:
        conn.close()
        return {"success": False, "error_msg": str(e)}

def self_correct_sql(prompt, bad_sql, error_msg):
    """Gửi truy vấn lỗi và thông báo lỗi SQLite trở lại Qwen LLM để sửa đổi câu lệnh SQL (không suy nghĩ)."""
    correction_prompt = f"{prompt}\n\n### Previous SQL Query\n{bad_sql}\n\n### Execution Error Message\n{error_msg}\n\nYour previous SQL query failed. Please correct the error and provide the revised SQLite SQL query wrapped in a ```sql ... ``` block."
    attempts = 0
    while True:
        try:
            response = chat_client.chat.completions.create(
                model="Qwen3.6-27B-GGUF",
                messages=[
                    {"role": "system", "content": "You are a translation assistant from text to SQL. Correct the SQL query based on the execution error and return only the corrected SQL wrapped in a ```sql ... ``` block."},
                    {"role": "user", "content": correction_prompt}
                ],
                temperature=0.0,
                max_tokens=150,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}}
            )
            content = response.choices[0].message.content
            sql_match = re.search(r"```sql\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
            if sql_match:
                return sql_match.group(1).strip()
            return content.strip()
        except RateLimitError:
            attempts += 1
            sleep_time = (2 ** attempts) + random.random()
            print(f"[LLM API] Rate limit reached. Sleeping {sleep_time:.2f}s...")
            time.sleep(sleep_time)
        except Exception as e:
            attempts += 1
            if attempts > 5:
                return bad_sql
            sleep_time = (2 ** attempts) + random.random()
            time.sleep(sleep_time)

def result_eq(res_gt, res_pred, order_matters=False):
    """Check if the predicted execution output matches golden output."""
    if not res_gt and not res_pred:
        return True
    if len(res_gt) != len(res_pred):
        return False
    if order_matters:
        return res_gt == res_pred
    else:
        return set(res_gt) == set(res_pred)

def process_example(idx, ex, total_count, db_root_dir):
    """Processes a single example: retrieval, generation, correction, execution comparison."""
    db_id = ex['db_id']
    question = ex['question']
    gold_sql = ex.get('SQL', ex.get('query', ''))
    evidence = ex.get('evidence', '')
    
    db_path = os.path.join(db_root_dir, db_id, f"{db_id}.sqlite")
    
    try:
        # 1. Load cached database schema info (or populate cache)
        db_cache_info = get_or_create_db_cache(db_id, db_path)
        
        # 2. Schema Linking (Retrieve relevant schema)
        retrieved_schema = retrieve_schema(question, db_cache_info, top_k=15)
        
        # 3. Prompt Building
        prompt = build_prompt(question, retrieved_schema, db_cache_info["foreign_keys"], evidence)
        
        # 4. Initial Generation
        pred_sql = generate_sql(prompt)
        
        # 5. Simulated Execution & Self-Correction Loop
        exec_res = execute_sql(db_path, pred_sql)
        
        corrected = False
        loop_count = 0
        original_sql = pred_sql
        
        while not exec_res['success'] and loop_count < 2:
            corrected = True
            loop_count += 1
            pred_sql = self_correct_sql(prompt, pred_sql, exec_res['error_msg'])
            exec_res = execute_sql(db_path, pred_sql)
            
        # 6. Evaluation against Ground Truth SQL
        gold_res = execute_sql(db_path, gold_sql)
        
        is_correct = False
        if exec_res['success'] and gold_res['success']:
            order_matters = ("order by" in gold_sql.lower())
            is_correct = result_eq(gold_res['result'], exec_res['result'], order_matters=order_matters)
            
        status_str = "CORRECT" if is_correct else "INCORRECT"
        if corrected and exec_res['success']:
            status_str += " (CORRECTED)"
            
        with print_lock:
            print(f"[{idx+1}/{total_count}] DB: {db_id} | Status: {status_str}")
            
        return {
            "idx": idx,
            "db_id": db_id,
            "question": question,
            "gold_sql": gold_sql,
            "original_sql": original_sql,
            "final_pred_sql": pred_sql,
            "is_corrected": corrected,
            "is_correct": is_correct,
            "error_msg": exec_res.get("error_msg") if not exec_res["success"] else None
        }
        
    except Exception as e:
        with print_lock:
            print(f"[{idx+1}/{total_count}] Error processing {db_id} - Q: {question}: {e}")
        return {
            "idx": idx,
            "db_id": db_id,
            "question": question,
            "gold_sql": gold_sql,
            "original_sql": "",
            "final_pred_sql": "",
            "is_corrected": False,
            "is_correct": False,
            "error_msg": str(e)
        }

def main():
    print("====================================================")
    print("LitE-SQL: Starting Full Spider Evaluation (1034 Qs)")
    print("====================================================")
    
    # Load validation data
    dev_path = "d:/Projects/LitE-SQL/datasets/spider/dev/dev.json"
    db_root = "d:/Projects/LitE-SQL/datasets/spider/dev/database"
    
    with open(dev_path, "r", encoding="utf-8") as f:
        dev_data = json.load(f)
        
    total_count = len(dev_data)
    print(f"Total validation queries to evaluate: {total_count}")
    
    # Load checkpoints if they exist
    evaluated_questions = {}
    results = []
    
    if os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                results = json.load(f)
            # Create a lookup set/dict for evaluated questions
            for r in results:
                key = (r["db_id"], r["question"])
                evaluated_questions[key] = r
            print(f"Found checkpoint with {len(results)} already evaluated queries. Resuming...")
        except Exception as e:
            print(f"Error loading checkpoint: {e}. Starting fresh.")
            results = []
            
    # Filter remaining work
    remaining_work = []
    for idx, ex in enumerate(dev_data):
        key = (ex["db_id"], ex["question"])
        if key in evaluated_questions:
            continue
        remaining_work.append((idx, ex))
        
    print(f"Remaining queries to run: {len(remaining_work)}")
    
    if not remaining_work:
        print("All queries have already been evaluated!")
        # Calculate accuracy from checkpoint
        correct_count = sum(1 for r in results if r["is_correct"])
        acc = correct_count / total_count * 100
        print(f"Final Accuracy: {acc:.2f}% ({correct_count}/{total_count})")
        return
        
    # Run evaluation with a thread pool
    max_workers = 8
    print(f"Starting ThreadPoolExecutor with {max_workers} workers...")
    
    start_time = time.time()
    
    # We will submit tasks
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_example, idx, ex, total_count, db_root): (idx, ex)
            for idx, ex in remaining_work
        }
        
        for future in as_completed(futures):
            res = future.result()
            
            # Save thread-safely
            with file_lock:
                results.append(res)
                # Save checkpoint after every finished query
                with open(checkpoint_path, "w", encoding="utf-8") as f:
                    json.dump(results, f, indent=4)
                    
            # Periodic status logging
            completed_count = len(results)
            if completed_count % 10 == 0 or completed_count == total_count:
                correct_count = sum(1 for r in results if r["is_correct"])
                current_acc = (correct_count / completed_count) * 100
                elapsed = time.time() - start_time
                speed = completed_count / elapsed if elapsed > 0 else 0
                eta = (total_count - completed_count) / speed if speed > 0 else 0
                
                with print_lock:
                    print("------------------------------------------------------------------------")
                    print(f"Progress: {completed_count}/{total_count} ({completed_count/total_count*100:.1f}%) | "
                          f"Correct: {correct_count} | "
                          f"Current Accuracy: {current_acc:.2f}% | "
                          f"Speed: {speed:.2f} Q/s | "
                          f"ETA: {eta/60:.1f} min")
                    print("------------------------------------------------------------------------")
                    
    # Final Summary
    total_elapsed = time.time() - start_time
    correct_count = sum(1 for r in results if r["is_correct"])
    final_acc = (correct_count / total_count) * 100
    
    print("\n====================================================")
    print("Spider Evaluation Completed!")
    print(f"Total Time: {total_elapsed/60:.1f} minutes")
    print(f"Execution Accuracy (EX): {final_acc:.2f}% ({correct_count}/{total_count})")
    print("====================================================")
    
if __name__ == "__main__":
    main()
