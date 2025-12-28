import numpy as np
import dashscope
from http import HTTPStatus
import base64
from PIL import Image
import io
import time
import os
from models import ProductImage, Product, db
from sqlalchemy import text

# 设置DashScope API密钥
dashscope.api_key = os.getenv("DASHSCOPE_API_KEY")
if not dashscope.api_key:
    # 简单的 warning，应用启动时不要直接 crash，这也是无状态的好处
    print("Warning: DASHSCOPE_API_KEY environment variable not set.")

class ImageSearchService:
    def __init__(self):
        """
        初始化图片搜索服务
        无状态设计：无需加载向量到内存
        """
        pass
        
    def _image_to_base64(self, image_path: str, max_size_mb: float = 2.5) -> str:
        """
        将图片转换为base64格式，如果图片太大会自动压缩
        Args:
            image_path: 图片路径
            max_size_mb: 最大文件大小（MB），超过此大小会压缩
        """
        # 读取图片
        image = Image.open(image_path)

        # 转换为RGB（如果需要）
        if image.mode not in ('RGB', 'L'):
            image = image.convert('RGB')

        # 先尝试以原始质量保存
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG', quality=95)
        img_bytes = img_byte_arr.getvalue()

        # 如果图片太大，进行压缩
        max_size_bytes = int(max_size_mb * 1024 * 1024)
        if len(img_bytes) > max_size_bytes:
            print(f"  图片大小 {len(img_bytes)/1024/1024:.2f}MB，需要压缩...")
            # 计算需要缩小的比例
            width, height = image.size
            scale_factor = (max_size_bytes / len(img_bytes)) ** 0.5 * 0.9  # 0.9 作为安全系数
            new_width = int(width * scale_factor)
            new_height = int(height * scale_factor)

            # 调整图片大小
            image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)

            # 重新保存
            img_byte_arr = io.BytesIO()
            quality = 85
            while quality > 50:
                img_byte_arr.seek(0)
                img_byte_arr.truncate()
                image.save(img_byte_arr, format='JPEG', quality=quality)
                img_bytes = img_byte_arr.getvalue()
                if len(img_bytes) <= max_size_bytes:
                    break
                quality -= 5

            print(f"  压缩后大小: {len(img_bytes)/1024/1024:.2f}MB，质量: {quality}")

        base64_image = base64.b64encode(img_bytes).decode('utf-8')
        return f"data:image/jpeg;base64,{base64_image}"
    
    def extract_feature(self, image_path: str) -> np.ndarray:
        """使用DashScope API提取图片特征向量"""
        # 将图片转换为base64格式
        print(f"正在处理图片: {image_path}")
        image_data = self._image_to_base64(image_path)
        
        # 调用DashScope API
        inputs = [{'image': image_data}]
        print("正在调用DashScope API...")
        
        # 添加重试机制
        max_retries = 3
        retry_delay = 5  # 初始重试延迟（秒）
        
        for retry in range(max_retries):
            try:
                resp = dashscope.MultiModalEmbedding.call(
                    model="multimodal-embedding-v1",
                    input=inputs
                )
                
                if resp.status_code != HTTPStatus.OK:
                    if "rate limit exceeded" in resp.message.lower():
                        if retry < max_retries - 1:  # 如果不是最后一次重试
                            print(f"API速率限制错误，等待 {retry_delay} 秒后重试 ({retry+1}/{max_retries})...")
                            time.sleep(retry_delay)
                            retry_delay *= 2  # 指数退避策略
                            continue
                    raise Exception(f"API调用失败: {resp.message}")
                
                # 获取特征向量
                print("API调用成功，正在处理返回结果...")
                # DashScope返回的已经是归一化的向量
                feature = np.array(resp.output['embeddings'][0]['embedding'], dtype=np.float32)
                return feature
                
            except Exception as e:
                if retry < max_retries - 1 and "rate limit exceeded" in str(e).lower():
                    print(f"API速率限制错误，等待 {retry_delay} 秒后重试 ({retry+1}/{max_retries})...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # 指数退避策略
                else:
                    raise  # 如果是其他错误或已达到最大重试次数，则抛出异常

    def search_similar_images(self, image_path: str, top_k: int = 10) -> list:
        """
        直接使用数据库进行以图搜图 (PostgreSQL + pgvector)
        """
        try:
            # 1. 提取特征向量
            query_vector = self.extract_feature(image_path)
            
            # 2. 数据库向量检索
            # 使用 pgvector 的 <-> 操作符 (L2距离)
            # 需要将 numpy 数组转换为列表，SQLAlchemy 会自动处理转换
            
            print(f"Executing SQL vector search for top {top_k} results...")
            
            results = db.session.query(
                ProductImage,
                ProductImage.vector.l2_distance(query_vector).label('distance')
            ).join(
                Product, ProductImage.model_number == Product.model_number
            ).order_by(
                ProductImage.vector.l2_distance(query_vector)
            ).limit(top_k).all()

            # 3. 格式化结果
            final_results = []
            for img, distance in results:
                # 将距离转换为相似度 (0-1)
                # distance 是 float 类型
                similarity = 1 / (1 + distance)
                
                final_results.append({
                    'model_number': img.model_number,
                    'image_path': img.image_path,
                    'original_path': img.original_path,
                    'oss_path': img.oss_path,
                    'similarity': float(similarity)
                })
            
            return final_results

        except Exception as e:
            print(f"搜索失败: {e}")
            import traceback
            traceback.print_exc()
            return []
