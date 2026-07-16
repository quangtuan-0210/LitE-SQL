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
    api_key="f0GVKMTEP4VBkjjAymxyYftvi6xX2mVz",
    timeout=20.0
)

chat_client = OpenAI(
    base_url="https://appresearchpublic83.aiplatform.vcntt.tech/v1",
    api_key="sglang",
    timeout=30.0
)

# Custom Embedding Function with Exponential Backoff
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
checkpoint_path = "d:/Projects/LitE-SQL/datasets/bird/dev_20240627/evaluation_checkpoint.json"

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
            
    if documents:
        embeddings = get_embeddings(documents)
        collection.add(
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
            ids=ids
        )
        
    db_info = {
        "schema": schema,
        "foreign_keys": foreign_keys,
        "collection": collection,
        "doc_count": len(documents)
    }
    
    with cache_lock:
        if db_id in db_cache:
            chroma_client.delete_collection(name=col_name)
            return db_cache[db_id]
        db_cache[db_id] = db_info
        return db_info

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
        fk_text = "\n-- Foreign Keys\n" + "\n".join(foreign_keys)
        parts.append(fk_text)
        
    if evidence and str(evidence).strip().lower() != 'nan' and str(evidence).strip() != '':
        parts.append(f"\n-- Evidence\n{evidence.strip()}")
        
    parts.append("\n### SQL (Return only the SQL query, wrapped in ```sql ... ``` block)\n")
    return "\n".join(parts)

def call_qwen_api(prompt):
    """Gửi yêu cầu tới mô hình Qwen-27B qua API để dịch câu hỏi sang truy vấn SQLite SQL (không suy nghĩ)."""
    messages = [
        {"role": "system", "content": "You are a precise database administrator. Translate the question into an executable SQL query. Return only the SQL query wrapped inside a single ```sql ``` code block."},
        {"role": "user", "content": prompt}
    ]
    
    attempts = 0
    while attempts < 5:
        try:
            response = chat_client.chat.completions.create(
                model="Qwen3.6-27B-GGUF",
                messages=messages,
                max_tokens=150,
                temperature=0.0,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}}
            )
            content = response.choices[0].message.content
            return content
        except Exception as e:
            attempts += 1
            print(f"[Qwen API] Attempt {attempts} failed: {e}. Retrying in 2s...")
            time.sleep(2)
            
    raise Exception("Qwen API failed after 5 retries.")

def extract_sql_from_response(response_text):
    """Trích xuất truy vấn SQL sạch từ khối phản hồi markdown của mô hình."""
    # Find ```sql ... ```
    match = re.search(r"```sql\s*(.*?)\s*```", response_text, re.DOTALL | re.IGNORECASE)
    if match:
        sql = match.group(1).strip()
    else:
        # Fallback to finding any block ``` ... ```
        match_any = re.search(r"```\s*(.*?)\s*```", response_text, re.DOTALL)
        if match_any:
            sql = match_any.group(1).strip()
        else:
            sql = response_text.strip()
            
    # Remove trailing semicolons or markdown artifacts
    sql = sql.rstrip(';').strip()
    return sql

def execute_sql(db_path, sql):
    """Thực thi câu lệnh SQL trên cơ sở dữ liệu SQLite và trả về kết quả hoặc thông báo lỗi."""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(sql)
        res = cursor.fetchall()
        conn.close()
        return {"success": True, "result": res, "error_msg": None}
    except Exception as e:
        return {"success": False, "result": None, "error_msg": str(e)}

def self_correct_sql(prompt, bad_sql, error_msg):
    """Gửi truy vấn lỗi và thông báo lỗi SQLite trở lại Qwen LLM để sửa đổi câu lệnh SQL (không suy nghĩ)."""
    correction_prompt = (
        f"{prompt}\n\n"
        f"### Previous Generated SQL\n{bad_sql}\n\n"
        f"### Execution Error\n{error_msg}\n\n"
        f"### Instruction\nThe previous SQL query generated the execution error shown above. Correct the query to fix this error. Return ONLY the corrected SQL query wrapped in a ```sql ... ``` block."
    )
    resp = call_qwen_api(correction_prompt)
    return extract_sql_from_response(resp)

def clean_row(row):
    """Convert cells to uniform format for order-insensitive comparison."""
    cleaned = []
    for cell in row:
        if cell is None:
            cleaned.append("None")
        elif isinstance(cell, float):
            cleaned.append(f"{cell:.4f}")
        else:
            cleaned.append(str(cell).strip())
    return tuple(cleaned)

def result_eq(gold_res, pred_res, order_matters=False):
    """Compare SQLite result sets for execution equality."""
    if len(gold_res) != len(pred_res):
        return False
        
    g_cleaned = [clean_row(r) for r in gold_res]
    p_cleaned = [clean_row(r) for r in pred_res]
    
    if order_matters:
        return g_cleaned == p_cleaned
    else:
        return sorted(g_cleaned) == sorted(p_cleaned)

def process_example(idx, ex, total_count, db_root):
    """Run full schema retrieval, generation, correction, and evaluation for one query."""
    db_id = ex["db_id"]
    question = ex["question"]
    gold_sql = ex["SQL"]
    evidence = ex.get("evidence", "")
    
    # Path to db: dev_databases/{db_id}/{db_id}.sqlite
    db_path = os.path.join(db_root, db_id, f"{db_id}.sqlite")
    
    try:
        # 1. Load schema cache
        db_cache_info = get_or_create_db_cache(db_id, db_path)
        
        # 2. Schema retrieval
        retrieved_schema = retrieve_schema(question, db_cache_info, top_k=15)
        
        # 3. Build Prompt
        prompt = build_prompt(question, retrieved_schema, db_cache_info["foreign_keys"], evidence)
        
        # 4. Generate SQL
        raw_resp = call_qwen_api(prompt)
        pred_sql = extract_sql_from_response(raw_resp)
        original_sql = pred_sql
        
        # 5. Execute and Self-Correct
        exec_res = execute_sql(db_path, pred_sql)
        corrected = False
        
        if not exec_res['success']:
            corrected = True
            # Attempt correction
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
    print("LitE-SQL: Starting BIRD Mini-Dev Evaluation")
    print("====================================================")
    
    # Load BIRD validation data
    dev_path = "d:/Projects/LitE-SQL/datasets/bird/dev_20240627/dev.json"
    db_root = "d:/Projects/LitE-SQL/datasets/bird/dev_20240627/dev_databases"
    
    if not os.path.exists(dev_path):
        print(f"Error: BIRD dev.json not found at {dev_path}")
        return
        
    with open(dev_path, "r", encoding="utf-8") as f:
        dev_data = json.load(f)
        
    total_count = len(dev_data)
    print(f"Total BIRD queries to evaluate: {total_count}")
    
    # Load checkpoints if they exist
    evaluated_questions = {}
    results = []
    
    os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)
    if os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                results = json.load(f)
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
        correct_count = sum(1 for r in results if r["is_correct"])
        acc = correct_count / total_count * 100
        print(f"Final Accuracy: {acc:.2f}% ({correct_count}/{total_count})")
        return
        
    print("Starting sequential evaluation in the main thread...")
    start_time = time.time()
    
    for idx, ex in remaining_work:
        res = process_example(idx, ex, total_count, db_root)
        
        # Save results
        results.append(res)
        with open(checkpoint_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=4)
            
        # Periodic status logging
        completed_count = len(results)
        if completed_count % 5 == 0 or completed_count == total_count:
            correct_count = sum(1 for r in results if r["is_correct"])
            current_acc = (correct_count / completed_count) * 100
            elapsed = time.time() - start_time
            speed = completed_count / elapsed if elapsed > 0 else 0
            eta = (total_count - completed_count) / speed if speed > 0 else 0
            
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
    print("BIRD Evaluation Completed!")
    print(f"Total Time: {total_elapsed/60:.1f} minutes")
    print(f"Execution Accuracy (EX): {final_acc:.2f}% ({correct_count}/{total_count})")
    print("====================================================")
    
if __name__ == "__main__":
    main()
