-- 用户注册自动创建资料触发器
-- 在 Supabase SQL Editor 中执行此脚本

-- 创建触发器函数: 当新用户注册时自动创建 user_profile
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO public.user_profiles (id, email, full_name, role)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(NEW.raw_user_meta_data->>'full_name', ''),
        COALESCE(NEW.raw_user_meta_data->>'role', 'employee')
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- 创建触发器: 在 auth.users 插入新用户后触发
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW
    EXECUTE FUNCTION public.handle_new_user();

-- 注释:
-- 1. 当用户通过 Supabase Auth 注册时，此触发器会自动在 user_profiles 表中创建对应记录
-- 2. 默认角色为 'employee'，除非在注册时通过 user_metadata 指定了 role
-- 3. 可以通过以下方式注册时指定角色:
--    supabase.auth.signUp({
--      email: 'user@example.com',
--      password: 'password',
--      options: {
--        data: { full_name: 'John Doe', role: 'admin' }
--      }
--    })
