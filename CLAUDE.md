# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**电子产品配件图像搜索系统 (Electronic Accessories Image Search System)** - AI-powered image search and product management system designed for camera straps, lanyards, and other electronic product accessories. Implements reverse image search (以图搜图) using DashScope embeddings and FAISS vector similarity search.

**Key Business Context**:
- Target users: Cross-border e-commerce sellers, wholesale buyers
- Use case: Quickly find products matching customer inquiry images
- Core feature: Upload an image → find visually similar products in database
- Data ownership: Local data storage for security and privacy

## Architecture

### Backend (Flask + Python)
- **Framework**: Flask with SQLAlchemy ORM
- **Database**: MySQL (`xiangyipackage_test`)
- **AI Pipeline**:
  - DashScope multimodal embedding API generates 1024-dimensional vectors
  - FAISS IndexFlatL2 performs L2 distance similarity search
  - Vector index rebuilt from database on application startup
- **Storage**: Local filesystem (`backend/uploads/product_images/{model_number}/`)
- **Optional**: Aliyun OSS cloud storage support

**Critical Files**:
- [app.py](backend/app.py) - Flask app initialization, database config, CORS, blueprint registration
- [product_search.py](backend/product_search.py) - `VectorProductIndex` class: FAISS indexing and similarity search
- [blueprints/products_v2.py](backend/blueprints/products_v2.py) - Product CRUD API (currently active version)
- [blueprints/product_search.py](backend/blueprints/product_search.py) - Image search API endpoints
- [models/product.py](backend/models/product.py) - `Product` and `ProductImage` SQLAlchemy models

**Vector Search Workflow**:
1. **Indexing**: Image upload → DashScope API → 1024-dim vector → Store in `product_images.vector` (BLOB)
2. **Search**: Query image → Generate embedding → FAISS similarity search → Return top-k products
3. **Startup**: Application loads all vectors from MySQL into in-memory FAISS index

### Frontend (React + TypeScript)
- **Framework**: React 18 with TypeScript, React Router
- **Build Tool**: Vite (dev server at `localhost:5173`)
- **UI Libraries**: Ant Design + Tailwind CSS
- **Core Features**:
  - Image search (reverse image lookup)
  - Product upload and management
  - CSV batch import with folder-based image loading

## Common Development Commands

### Backend Setup & Run
```bash
cd backend

# Virtual environment setup
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Environment configuration
cp .env.example .env
# Edit .env: set DASHSCOPE_API_KEY and MySQL credentials

# Initialize database (choose one method)
python init_new_db.py  # Python ORM approach
# OR
mysql -u root -p < init_database.sql  # SQL script approach

# Start development server
python app.py  # Runs on http://0.0.0.0:5000

# Run tests
python -m pytest test/ -v
```

### Frontend Setup & Run
```bash
cd frontend

# Install dependencies
npm install  # or yarn install

# Development server
npm run dev  # Opens localhost:5173

# Production build
npm run build

# Lint code
npm run lint
```

### Docker Deployment
```bash
# Build and start full stack (MySQL + Backend + Frontend)
docker-compose up --build

# Run in background
docker-compose up -d

# View logs
docker-compose logs -f backend
docker-compose logs -f db

# Stop containers
docker-compose down

# Reset database volumes (WARNING: deletes all data)
docker-compose down -v
```

## Environment Variables

Create `backend/.env` from `.env.example`:

**Required**:
- `DASHSCOPE_API_KEY` - Aliyun DashScope API key for image embeddings
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` - MySQL connection config
  - Default database name: `xiangyipackage_test`

**Optional**:
- `OSS_ACCESS_KEY_ID`, `OSS_ACCESS_KEY_SECRET`, `OSS_ENDPOINT`, `OSS_BUCKET_NAME` - Aliyun OSS storage
- `OSS_UPLOAD_BASE_PATH` - Restrict OSS batch uploads to specific local directory
- `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` - Additional AI capabilities
- `DATASET_ROOT` - Local dataset path (default: `./data/摄像师拍摄素材`)

## Database Schema

**Database Name**: `xiangyipackage_test`
**Character Set**: `utf8mb4_unicode_ci`

### `products` Table (Electronic Accessories)

**Primary Key**: `model_number` (VARCHAR, not auto-increment INT)

| Field | Type | Constraint | Description |
|-------|------|------------|-------------|
| model_number | VARCHAR(100) | PRIMARY KEY | Product model number |
| photographer_file | VARCHAR(255) | NOT NULL | Photographer filename reference |
| alibaba_product_url | VARCHAR(500) | NOT NULL | 1688/Alibaba product URL |
| category | VARCHAR(100) | NOT NULL | Product category |
| spec_cn_reference | TEXT | NULL | Chinese spec (reference) |
| spec_cn | TEXT | NULL | Chinese specification |
| spec_en | TEXT | NULL | English specification |
| product_size | VARCHAR(200) | NULL | Product dimensions |
| package_size | VARCHAR(200) | NULL | Package dimensions |
| price_1688 | DECIMAL(10,2) | NULL | 1688 wholesale price |
| fob_price_tier1 | DECIMAL(10,2) | NULL | FOB price 300-1999 units |
| fob_price_tier2 | DECIMAL(10,2) | NULL | FOB price 2000-9999 units |
| fob_price_tier3 | DECIMAL(10,2) | NULL | FOB price >=10000 units |
| intl_platform_price | DECIMAL(10,2) | NULL | International platform pricing |
| competitor_price | DECIMAL(10,2) | NULL | Competitor pricing reference |
| ref_link_1/2/3 | VARCHAR(500) | NULL | Reference links |
| intl_platform_url* | VARCHAR(500) | NULL | International platform URLs |
| created_at, updated_at | TIMESTAMP | AUTO | Timestamps |

**Indexes**: `idx_category`, `idx_created_at`

### `product_images` Table (Product Image Vectors)

**Primary Key**: `id` (INT, auto-increment)
**Foreign Key**: `model_number` → `products.model_number` (CASCADE DELETE)

| Field | Type | Constraint | Description |
|-------|------|------------|-------------|
| id | INT | PK, AUTO_INCREMENT | Image ID |
| model_number | VARCHAR(100) | FK, NOT NULL | Links to product |
| image_path | VARCHAR(255) | UNIQUE, NOT NULL | Web-accessible path |
| vector | BLOB | NOT NULL | 1024-dim embedding (numpy array) |
| original_path | TEXT | NULL | Filesystem absolute path |
| oss_path | TEXT | NULL | Aliyun OSS path (optional) |
| image_order | INT | DEFAULT 0 | Display order |
| is_primary | BOOLEAN | DEFAULT FALSE | Primary product image flag |
| created_at | TIMESTAMP | AUTO | Creation timestamp |

**Indexes**: `unique_image_path`, `idx_model_number`, `idx_is_primary`

## Vector Search Implementation

**Core Class**: `VectorProductIndex` ([backend/product_search.py](backend/product_search.py))

**Key Methods**:
```python
extract_feature(image_path: str) -> np.ndarray
    # Calls DashScope API to generate 1024-dim embedding

add_product(product_info: ProductInfo, image_path: str) -> dict
    # Adds product with image vector to MySQL + FAISS index

search_similar_images(image_path: str, top_k: int = 5) -> List[dict]
    # Performs FAISS similarity search, returns top-k products

_load_vectors()
    # Loads all vectors from MySQL into FAISS index at startup
```

**Important Implementation Details**:
- FAISS index is **not persisted to disk** - database is single source of truth
- Vectors stored as BLOB in MySQL (serialized numpy arrays)
- Index rebuilt from scratch on each application startup
- Uses L2 distance metric (`IndexFlatL2`)

## CSV Import Format

CSV files must include these columns (first 5 are required):

```csv
model_number,photographer_file,alibaba_product_url,category,图片路径,参数中文,参数英文,产品尺寸,包装尺寸,1688价格,FOB报价1,FOB报价2,FOB报价3,国际站定价,同行定价,链接1,链接2,链接3,国际站,国际站1,国际站2
```

**Required Fields**:
1. `model_number` - Unique product identifier
2. `photographer_file` - Photographer reference
3. `alibaba_product_url` - Product source URL
4. `category` - Product category
5. `图片路径` - Image path (relative to dataset root or absolute)

## Database Initialization

### Method 1: SQL Script (Recommended)
```bash
cd backend
mysql -u root -p < init_database.sql
```

### Method 2: Python ORM
```bash
cd backend
python init_new_db.py
```

### Verification
```bash
mysql -u root -p xiangyipackage_test -e "
SHOW TABLES;
DESCRIBE products;
DESCRIBE product_images;
SELECT COUNT(*) FROM products;
"
```

### Docker Environment
```bash
# Execute SQL in running container
docker exec -i fashion-crm-db mysql -uroot -p${DB_PASSWORD} < backend/init_database.sql
```

## Testing

```bash
cd backend

# Run all tests
python -m pytest test/ -v

# Test specific module
python -m pytest test/test_product_search.py -v

# With coverage
python -m pytest test/ --cov=. --cov-report=html
```

## Important Architecture Notes

**Primary Key Change**:
- ⚠️ Products use `model_number` (VARCHAR) as primary key, NOT auto-increment `id`
- All foreign key relationships reference `model_number`
- API endpoints should use `model_number` in URLs

**File Upload Constraints**:
- Max file size: 16MB (configured in `app.py`)
- Allowed formats: png, jpg, jpeg, gif, webp
- Storage path: `backend/uploads/product_images/{model_number}/`

**Blueprint Versions**:
- `products_v2.py` is the **active** blueprint (registered in app.py)
- `products.py` is legacy code (may need deletion or archival)
- `customers.py`, `orders.py` are **legacy** - no longer used (product-only system)

**FAISS Index Behavior**:
- Index is ephemeral (in-memory only)
- Database is authoritative source
- Application restart triggers full rebuild from MySQL
- No separate index persistence files

**Deployment Architecture** (Docker):
- `db` service: MySQL 8 with custom config in `mysql/conf.d/`
- `backend` service: Flask app with Gunicorn (production) or Flask dev server
- `frontend` service: Nginx serving static Vite build
- Port mapping: Frontend (80), Backend (5000), MySQL (3307→3306)
- Data persistence: `mysql_data` volume, `backend/uploads/` bind mount
