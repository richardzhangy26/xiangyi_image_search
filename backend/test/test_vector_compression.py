#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试向量压缩方案
"""
import numpy as np
import zlib
import sys

# 生成测试向量
vector = np.random.randn(1024).astype('float32')
vector = vector / np.linalg.norm(vector)

# 方案1：原始存储（当前）
original_bytes = vector.tobytes()
original_size = len(original_bytes)

# 方案2：zlib 压缩
compressed_bytes = zlib.compress(original_bytes)
compressed_size = len(compressed_bytes)

# 方案3：半精度 float16（损失精度）
vector_fp16 = vector.astype('float16')
fp16_bytes = vector_fp16.tobytes()
fp16_size = len(fp16_bytes)

print("="*60)
print("向量存储方案对比")
print("="*60)
print(f"\n原始 float32: {original_size:,} 字节 (4096 B)")
print(f"zlib 压缩:    {compressed_size:,} 字节 (压缩率: {compressed_size/original_size*100:.1f}%)")
print(f"float16:      {fp16_size:,} 字节 (压缩率: {fp16_size/original_size*100:.1f}%)")

# 验证解压缩
decompressed = np.frombuffer(zlib.decompress(compressed_bytes), dtype='float32')
print(f"\n✓ 解压缩验证: {np.allclose(vector, decompressed)}")

# 验证 float16 精度损失
vector_fp16_back = vector_fp16.astype('float32')
error = np.mean(np.abs(vector - vector_fp16_back))
print(f"✓ float16 平均误差: {error:.6f}")

# 计算总存储空间节省
vectors_count = 2418
print(f"\n对于 {vectors_count} 个向量:")
print(f"float32 总大小: {original_size * vectors_count / 1024 / 1024:.2f} MB")
print(f"zlib 总大小:    {compressed_size * vectors_count / 1024 / 1024:.2f} MB")
print(f"float16 总大小: {fp16_size * vectors_count / 1024 / 1024:.2f} MB")
