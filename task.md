# Task List: Full Spider Evaluation

- [x] Download all 20 databases for Spider 1.0 validation set
- [x] Write the optimized evaluation script `run_full_evaluation.py` with sequential main-thread execution, rate limit handling, and checkpointing
- [x] Run a dry-run test to verify the script and checkpoint resuming works correctly
- [x] Run the full 1,034-query Spider evaluation
- [x] Analyze the results and update the walkthrough/report

## Future Optimizations (Cải tiến độ chính xác)

- [ ] Tích hợp Few-Shot Prompting (Truy xuất và chèn các ví dụ mẫu tương tự từ Train set bằng ChromaDB)
- [ ] Chuyển đổi định dạng Schema Prompt sang dạng DDL chuẩn (`CREATE TABLE`) thay vì JSON
- [ ] Phát triển Dynamic Value Retrieval (Tự động quét database SQLite để lấy giá trị lọc chính xác)
- [ ] Nâng cấp Schema Linking kết hợp Hybrid Search (Vector + Từ khóa BM25)
