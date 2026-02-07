-- 为现有产品表添加 RLS (Row Level Security) 策略
-- 在 Supabase SQL Editor 中执行此脚本

-- 启用 RLS
ALTER TABLE public.products ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.product_images ENABLE ROW LEVEL SECURITY;

-- ==================== products 表策略 ====================

-- 策略: 所有已认证用户可以查看产品
DROP POLICY IF EXISTS "Authenticated users can view products" ON public.products;
CREATE POLICY "Authenticated users can view products"
    ON public.products FOR SELECT
    TO authenticated
    USING (true);

-- 策略: 只有管理员可以创建产品
DROP POLICY IF EXISTS "Admins can insert products" ON public.products;
CREATE POLICY "Admins can insert products"
    ON public.products FOR INSERT
    TO authenticated
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.user_profiles
            WHERE id = auth.uid() AND role = 'admin'
        )
    );

-- 策略: 只有管理员可以更新产品
DROP POLICY IF EXISTS "Admins can update products" ON public.products;
CREATE POLICY "Admins can update products"
    ON public.products FOR UPDATE
    TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.user_profiles
            WHERE id = auth.uid() AND role = 'admin'
        )
    );

-- 策略: 只有管理员可以删除产品
DROP POLICY IF EXISTS "Admins can delete products" ON public.products;
CREATE POLICY "Admins can delete products"
    ON public.products FOR DELETE
    TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.user_profiles
            WHERE id = auth.uid() AND role = 'admin'
        )
    );

-- ==================== product_images 表策略 ====================

-- 策略: 所有已认证用户可以查看产品图片
DROP POLICY IF EXISTS "Authenticated users can view product images" ON public.product_images;
CREATE POLICY "Authenticated users can view product images"
    ON public.product_images FOR SELECT
    TO authenticated
    USING (true);

-- 策略: 只有管理员可以创建产品图片
DROP POLICY IF EXISTS "Admins can insert product images" ON public.product_images;
CREATE POLICY "Admins can insert product images"
    ON public.product_images FOR INSERT
    TO authenticated
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.user_profiles
            WHERE id = auth.uid() AND role = 'admin'
        )
    );

-- 策略: 只有管理员可以删除产品图片
DROP POLICY IF EXISTS "Admins can delete product images" ON public.product_images;
CREATE POLICY "Admins can delete product images"
    ON public.product_images FOR DELETE
    TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.user_profiles
            WHERE id = auth.uid() AND role = 'admin'
        )
    );

-- 测试 RLS 策略的辅助查询:
-- SELECT * FROM pg_policies WHERE tablename IN ('products', 'product_images');
-- SELECT * FROM pg_policies WHERE schemaname = 'public' AND tablename = 'products';
