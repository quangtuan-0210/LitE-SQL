# -*- coding: utf-8 -*-
"""
Tập lệnh chạy đánh giá baseline Text-to-SQL trên bộ dữ liệu FloodSQL-Bench
sử dụng framework LitE-SQL.

Các tính năng chính:
1. Schema Linking dựa trên ChromaDB Vector DB (Jina-embeddings-v3) để tìm các cột liên quan nhất.
2. DuckDB tích hợp phần mở rộng Không gian (Spatial Extension) để thực thi câu lệnh SQL địa lý thực tế.
3. LLM Qwen3.6-27B-GGUF sinh câu lệnh SQL địa lý.
4. Cơ chế tự sửa lỗi tự động (Self-Correction Loop) dựa trên phản hồi lỗi thực thi của DuckDB.
"""
import os
import json
import sqlite3
import random
import re
import time
import glob
import duckdb
import pandas as pd
import chromadb
from openai import OpenAI, RateLimitError

# Initialize OpenAI Clients
embedding_client = OpenAI(
    base_url="https://embedding.openclassroomteam.com/v1",
    api_key="f0GVKMTEP4VBkjjAymxyYftvi6xX2mVz"
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

# In-memory ChromaDB client
chroma_client = chromadb.Client()

def build_schema_index(meta):
    """Build ChromaDB column index from metadata.json."""
    col_name = f"flood_cols_{random.randint(1, 1000000)}"
    collection = chroma_client.create_collection(name=col_name)
    
    documents = []
    metadatas = []
    ids = []
    idx = 0
    
    for table_name, table_info in meta.items():
        if table_name == "_global":
            continue
        for col in table_info.get("schema", []):
            cname = col.get("column_name", "")
            cdesc = col.get("description", "")
            ctype = col.get("data_type", "TEXT")
            doc_text = f"Table: {table_name}, Column: {cname}, Type: {ctype}, Description: {cdesc}"
            documents.append(doc_text)
            metadatas.append({"table": table_name, "column": cname, "type": ctype, "description": cdesc})
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
        
    return collection, len(documents)

def retrieve_schema(question, collection, doc_count, meta_schema, top_k_per_table=8):
    """Retrieve top columns per table based on the question vector."""
    if doc_count == 0:
        return {}
        
    q_emb = get_embeddings([question])[0]
    
    # 1. Identify which tables are relevant to the question
    global_results = collection.query(
        query_embeddings=[q_emb],
        n_results=min(25, doc_count)
    )
    chosen_tables = set()
    for meta in global_results['metadatas'][0]:
        chosen_tables.add(meta['table'])
        
    retrieved_schema = {}
    critical_cols = {"geoid", "countyfp", "statefp", "countyfips", "geometry", "lon", "lat", "dateofloss", "name"}
    
    # 2. For each chosen table, query ChromaDB using metadata filter to get top_k_per_table columns
    for table_name in chosen_tables:
        table_results = collection.query(
            query_embeddings=[q_emb],
            where={"table": table_name},
            n_results=top_k_per_table
        )
        
        retrieved_schema[table_name] = {}
        for meta in table_results['metadatas'][0]:
            c = meta['column']
            retrieved_schema[table_name][c] = {
                "type": meta["type"],
                "description": meta["description"]
            }
            
        # 3. Guarantee critical columns (join keys / geometry) are included
        for col_info in meta_schema[table_name].get("schema", []):
            cname = col_info.get("column_name", "")
            if cname.lower() in critical_cols and cname not in retrieved_schema[table_name]:
                retrieved_schema[table_name][cname] = {
                    "type": col_info.get("data_type", "TEXT"),
                    "description": col_info.get("description", "")
                }
                
    return retrieved_schema

SYSTEM_PROMPT = """You are an expert DuckDB SQL generator for the FloodSQL_Bench dataset.
Use only the tables and columns given in the metadata context.
Do NOT output any reasoning, explanation, or analysis.
Output only the final SQL query wrapped inside a ```sql ... ``` code block.
Do not output semicolons at the end of the query.

CRITICAL RULES FOR PERFORMANCE, JOIN KEYS & SPATIAL JOINS:
1. When performing a spatial join/filter (like ST_Intersects, ST_Contains, or ST_Within) between another table and the 'floodplain' table, if the query filters by state (e.g., STATEFP = '12' or GEOID starting with a state prefix), you MUST explicitly apply the same state filter to the 'floodplain' table as well (e.g., floodplain.STATEFP = '12' or floodplain.GEOID LIKE '12%'). Failing to do so causes a massive cross-join on the entire 2.5 GB floodplain dataset, leading to a query timeout!
2. Apply state/county filters on all joined tables as early as possible to minimize geometric calculation overhead.
3. ALWAYS apply `ST_IsValid(geometry)` filter in the WHERE clause for any tables involved in spatial joins, filters, or area calculations (e.g., `WHERE ST_IsValid(floodplain.geometry)`) to ensure the queries execute correctly.
4. When joining point-based tables (`hospitals`, `schools`) with polygon-based tables (`county`, `census_tracts`, `floodplain`), always perform a spatial join using `ST_Contains(polygon_table.geometry, ST_Point(point_table.LON, point_table.LAT))` or `ST_Within(ST_Point(point_table.LON, point_table.LAT), polygon_table.geometry)`.
5. Join keys for geographic tables:
   - To join `claims` or `cre` with `county`, use `LEFT(claims.GEOID, 5) = county.GEOID`.
   - To join `hospitals` or `schools` with `county`, use `LEFT(hospitals.COUNTYFIPS, 5) = county.GEOID`.
   - To join `claims` or `cre` with `census_tracts`, use `claims.GEOID = census_tracts.GEOID`.
6. To avoid NULL values in aggregations (e.g. SUM, AVG, MAX), always add the corresponding column `IS NOT NULL` condition in the WHERE clause (e.g., `amountPaidOnContentsClaim IS NOT NULL`).
7. The date of an NFIP claim in the 'claims' table is represented by the column 'dateOfLoss'. Always use 'dateOfLoss' for any time or date filters/comparisons on claims (e.g. claims.dateOfLoss >= '2015-01-01').
8. Pay close attention to spatial relationships in the question:
   - "located within", "fall inside", "are inside" -> Use `ST_Within(inner.geometry, outer.geometry)` or `ST_Contains(outer.geometry, inner.geometry)`. Do NOT use `ST_Intersects`!
   - "intersect", "overlap", "touch", "border" -> Use `ST_Intersects(geometry1, geometry2)`.
   - "share boundaries", "touch boundaries" -> Use `ST_Touches(geometry1, geometry2)` or `ST_Intersects` with boundary checks."""

def build_prompt(question, retrieved_schema, meta):
    schema_lines = []
    for table_name, cols in retrieved_schema.items():
        schema_lines.append(f"Table: {table_name}")
        for col_name, info in cols.items():
            schema_lines.append(f"  Column: {col_name}, Type: {info['type']}, Description: {info['description']}")
            
    schema_text = "\n".join(schema_lines)
    
    # Extract global rules and joins
    global_info = []
    jr = meta.get("_global", {}).get("join_rules", {})
    global_info.append("### Key-Based Join Rules:")
    for k1 in ["direct", "concat"]:
        for it in jr.get("key_based", {}).get(k1, []):
            p = it.get("pair", [])
            if len(p) == 2:
                global_info.append(f"  - Join {p[0]} with {p[1]}")
    global_info.append("### Spatial Join Rules:")
    for k2 in ["point_polygon", "polygon_polygon"]:
        for it in jr.get("spatial", {}).get(k2, []):
            p = it.get("pair", [])
            if len(p) == 2:
                global_info.append(f"  - Spatial join {p[0]} with {p[1]}")
                
    global_info.append("### Query Rules:")
    for k, v in meta.get("_global", {}).get("rules", {}).items():
        global_info.append(f"  - {k}: {v}")
        
    global_text = "\n".join(global_info)
    
    prompt = f"""### Database Schema Context:
{schema_text}

### Database Rules & Joins Context:
{global_text}

### Question:
{question}

Return ONLY the DuckDB SQL query, wrapped inside a single ```sql ... ``` block."""
    return prompt

def clean_sql(content):
    content = content.strip()
    # Remove leading ```sql or ```
    if content.startswith("```sql"):
        content = content[6:]
    elif content.startswith("```"):
        content = content[3:]
    # Remove trailing ```
    if content.endswith("```"):
        content = content[:-3]
    return content.strip()

def generate_sql(prompt):
    while True:
        try:
            response = chat_client.chat.completions.create(
                model="Qwen3.6-27B-GGUF",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.0,
                max_tokens=400,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}}
            )
            content = response.choices[0].message.content
            return clean_sql(content)
        except Exception as e:
            print(f"[API Error] Calling Chat API failed: {e}. Retrying in 2s...")
            time.sleep(2)

def self_correct_sql(prompt, bad_sql, error_msg):
    correction_prompt = f"{prompt}\n\n### Previous SQL Query\n{bad_sql}\n\n### Execution Error Message\n{error_msg}\n\nYour previous SQL query failed. Please correct the error and provide the revised DuckDB SQL query wrapped in a ```sql ... ``` block."
    while True:
        try:
            response = chat_client.chat.completions.create(
                model="Qwen3.6-27B-GGUF",
                messages=[
                    {"role": "system", "content": "You are a translation assistant from text to SQL. Correct the SQL query based on the execution error and return only the corrected SQL wrapped in a ```sql ... ``` block. Do not reason. Do not think. Output the SQL query immediately."},
                    {"role": "user", "content": correction_prompt}
                ],
                temperature=0.0,
                max_tokens=400,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}}
            )
            content = response.choices[0].message.content
            return clean_sql(content)
        except Exception as e:
            print(f"[API Error] Calling Chat API failed: {e}. Retrying in 2s...")
            time.sleep(2)

def execute_sql(con, sql, timeout_sec=15.0):
    """Execute SQL on DuckDB connection with interrupt timeout handler."""
    import threading
    interrupted = False
    query_finished = False
    lock = threading.Lock()
    
    def interrupt_func():
        nonlocal interrupted
        with lock:
            if query_finished:
                return
            interrupted = True
            try:
                con.interrupt()
            except Exception:
                pass

    timer = threading.Timer(timeout_sec, interrupt_func)
    try:
        timer.start()
        df = con.execute(sql).fetchdf()
        with lock:
            query_finished = True
            if interrupted:
                raise duckdb.InterruptException("Query timed out")
        return {"success": True, "result": df, "error_msg": None}
    except Exception as e:
        with lock:
            query_finished = True
        return {"success": False, "result": None, "error_msg": str(e)}
    finally:
        with lock:
            query_finished = True
        timer.cancel()

def compare_results(df1, df2):
    if df1 is None or df2 is None:
        return False
    if df1.shape != df2.shape:
        return False
        
    df2.columns = df1.columns
    df1 = df1.sort_values(by=df1.columns.tolist()).reset_index(drop=True)
    df2 = df2.sort_values(by=df2.columns.tolist()).reset_index(drop=True)
    
    try:
        pd.testing.assert_frame_equal(df1, df2, check_dtype=False, check_exact=False, atol=1e-3)
        return True
    except AssertionError:
        return False

def main():
    print("====================================================")
    print("LitE-SQL: Starting FloodSQL-Bench Evaluation")
    print("====================================================")
    
    # 1. Setup paths
    flood_root = "D:/Projects/FloodSQL-Bench-main"
    data_dir = os.path.join(flood_root, "data")
    metadata_path = os.path.join(data_dir, "metadata_parquet.json")
    benchmark_path = os.path.join(flood_root, "benchmark", "bechmark_updated.jsonl")
    checkpoint_path = "d:/Projects/LitE-SQL/results/FloodSQL_LitE-SQL_checkpoint.json"
    results_path = "d:/Projects/LitE-SQL/results/FloodSQL_LitE-SQL_results.json"
    
    os.makedirs("d:/Projects/LitE-SQL/results", exist_ok=True)
    
    # 2. Load metadata and build ChromaDB index
    with open(metadata_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    print("Building schema index in ChromaDB...")
    collection, doc_count = build_schema_index(meta)
    print(f"ChromaDB Schema Index built with {doc_count} columns.")
    
    # 3. Setup DuckDB database views
    print("Initializing DuckDB with Spatial extension...")
    con = duckdb.connect(database=':memory:')
    con.execute("INSTALL spatial; LOAD spatial;")
    for filepath in glob.glob(os.path.join(data_dir, "*.parquet")):
        filename = os.path.basename(filepath)
        table_name = filename.replace('.parquet', '').replace('_tx_fl_la', '')
        con.execute(f"CREATE OR REPLACE VIEW {table_name} AS SELECT * FROM '{filepath}'")
    print("DuckDB views created successfully.")
    
    # 4. Load benchmark dataset
    with open(benchmark_path, "r", encoding="utf-8") as f:
        queries = [json.loads(line) for line in f]
        
    total_queries = len(queries)
    print(f"Loaded {total_queries} queries from FloodSQL benchmark.")
    
    # 5. Load checkpoint
    evaluated_records = []
    if os.path.exists(checkpoint_path):
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            evaluated_records = json.load(f)
    processed_ids = {r["id"] for r in evaluated_records}
    correct_count = sum(1 for r in evaluated_records if r["is_correct"])
    
    if processed_ids:
        print(f"Resuming evaluation. Found checkpoint with {len(processed_ids)} already evaluated queries. Current correct: {correct_count}")
        
    start_time = time.time()
    
    for idx, item in enumerate(queries):
        qid = item["id"]
        if qid in processed_ids:
            continue
            
        question = item["question"]
        gold_sql = item["sql"]
        
        print(f"\n[{idx+1}/{total_queries}] DB: FloodSQL | Question: {question}")
        
        # 1. Schema Linking (retrieve Top-15 columns)
        retrieved_schema = retrieve_schema(question, collection, doc_count, meta, top_k_per_table=8)
        
        # 2. Build Prompt
        prompt = build_prompt(question, retrieved_schema, meta)
        
        # 3. Generate SQL
        pred_sql = generate_sql(prompt)
        print(f"  -> Generated SQL: {pred_sql[:100]}...")
        
        # 4. Execute and Self-Correction Loop
        exec_res = execute_sql(con, pred_sql)
        attempts = 0
        while not exec_res["success"] and attempts < 2:
            attempts += 1
            print(f"  -> Error: {exec_res['error_msg']}. Self-correcting (Attempt {attempts}/2)...")
            pred_sql = self_correct_sql(prompt, pred_sql, exec_res["error_msg"])
            exec_res = execute_sql(con, pred_sql)
            
        # 5. Evaluate correctness
        is_correct = False
        gold_res = execute_sql(con, gold_sql)
        
        if exec_res["success"] and gold_res["success"]:
            is_correct = compare_results(gold_res["result"], exec_res["result"])
            
        if is_correct:
            correct_count += 1
            status_str = "CORRECT"
        else:
            status_str = "INCORRECT"
            
        print(f"  -> Status: {status_str} (Accuracy so far: {correct_count}/{len(processed_ids)+1} - {correct_count/(len(processed_ids)+1)*100:.2f}%)")
        
        # Save record
        record = {
            "id": qid,
            "question": question,
            "gold_sql": gold_sql,
            "pred_sql": pred_sql,
            "is_correct": is_correct,
            "error_msg": exec_res["error_msg"]
        }
        evaluated_records.append(record)
        processed_ids.add(qid)
        
        # Write checkpoint
        with open(checkpoint_path, "w", encoding="utf-8") as f:
            json.dump(evaluated_records, f, indent=4, ensure_ascii=False)
            
    # Clean up and save final results
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(evaluated_records, f, indent=4, ensure_ascii=False)
        
    # Delete temporary checkpoint
    if os.path.exists(checkpoint_path):
        os.remove(checkpoint_path)
        
    elapsed = time.time() - start_time
    final_acc = (correct_count / total_queries) * 100
    
    print("\n====================================================")
    print("FloodSQL-Bench Evaluation Completed!")
    print(f"Total Time: {elapsed/60:.2f} minutes")
    print(f"Execution Accuracy (EX): {final_acc:.2f}% ({correct_count}/{total_queries} correct)")
    print("====================================================")

if __name__ == "__main__":
    main()
