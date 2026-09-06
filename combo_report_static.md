# StaticOverlay 纯静态特效组合测试报告（33个）

**类型**: StaticOverlay（纯静态叠加）
**说明**: 仅使用类型1（静态）动作，不包含相机运镜（focus_pan/dolly）。所有特效在单遍 FFmpeg 管道中叠加完成，不移动原视频内容。

生成时间: 2026-07-15 11:27:00

输入视频: D:\project\auto_video_gen\auto-video-gen\scenes\ceshi_apply\input.mp4

区域数量: 3

组合总数: 33

成功: 33, 失败/跳过: 0


## 组合名称对照表（中文 → 英文）

static_effect 字段支持两种写法：
1. **列表**：原子动作名数组，如 `["spotlight", "hl_border"]`
2. **字符串**：下表中的英文名，如 `"spotlight_border"`

| # | 中文名 | 英文名 | 原子动作 |
|---|--------|--------|----------|
| 1 | 聚光金边 | spotlight_border | spotlight + hl_border |
| 2 | 暗调金边 | dark_border | dark + hl_border |
| 3 | 暗调描边 | dark_outline | dark + animated_outline |
| 4 | 暗调箭头 | dark_arrow | dark + region_arrow |
| 5 | 暗调光晕 | dark_halo | dark + halo |
| 6 | 朦胧亮点 | annotate_dots | annotate_border + dot_motion |
| 7 | 多层光晕 | glow_halo | glow_border + halo |
| 8 | 聚光景深 | spotlight_depth | spotlight + depth_of_field |
| 9 | 暗调四角 | dark_brackets | dark + corner_brackets |
| 10 | 金边箭头 | border_arrow | hl_border + region_arrow |
| 11 | 虚化描边 | blur_outline | bg_blur + animated_outline |
| 12 | 揭示金边 | reveal_border | seq_reveal + hl_border |
| 13 | 聚光描边 | spotlight_outline | spotlight + animated_outline |
| 14 | 聚光四角 | spotlight_brackets | spotlight + corner_brackets |
| 15 | 聚光箭头 | spotlight_arrow | spotlight + region_arrow |
| 16 | 聚光光晕 | spotlight_halo | spotlight + halo |
| 17 | 暗调朦胧 | dark_annotate | dark + annotate_border |
| 18 | 暗调亮点 | dark_dots | dark + dot_motion |
| 19 | 金边光晕 | border_halo | hl_border + halo |
| 20 | 描边箭头 | outline_arrow | animated_outline + region_arrow |
| 21 | 描边四角 | outline_brackets | animated_outline + corner_brackets |
| 22 | 虚化金边 | blur_border | bg_blur + hl_border |
| 23 | 虚化四角 | blur_brackets | bg_blur + corner_brackets |
| 24 | 暗调金边箭头 | dark_border_arrow | dark + hl_border + region_arrow |
| 25 | 暗调描边四角 | dark_outline_brackets | dark + animated_outline + corner_brackets |
| 26 | 聚光金边光晕 | spotlight_border_halo | spotlight + hl_border + halo |
| 27 | 暗调朦胧亮点 | dark_annotate_dots | dark + annotate_border + dot_motion |
| 28 | 暗调金边光晕 | dark_border_halo | dark + hl_border + halo |
| 29 | 聚光金边箭头 | spotlight_border_arrow | spotlight + hl_border + region_arrow |
| 30 | 暗调描边箭头 | dark_outline_arrow | dark + animated_outline + region_arrow |
| 31 | 聚光描边四角 | spotlight_outline_brackets | spotlight + animated_outline + corner_brackets |
| 32 | 虚化描边四角 | blur_outline_brackets | bg_blur + animated_outline + corner_brackets |
| 33 | 聚光描边箭头 | spotlight_outline_arrow | spotlight + animated_outline + region_arrow |


## 汇总表

| # | 组合名 | 动作 | 状态 | 文件大小 | 耗时 | OSS URL |
|---|--------|------|------|---------|------|---------|
| 1 | 聚光金边 | spotlight + hl_border | success | 1020KB | 47.7s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/041ecbcf94ae4db09ce3e7808289d2ef.mp4 |
| 2 | 暗调金边 | dark + hl_border | success | 828KB | 49.9s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/94e56c235c3c46eba09dc1744d74d926.mp4 |
| 3 | 暗调描边 | dark + animated_outline | success | 847KB | 25.4s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/50d7bf30f217466fb539a3ae62c243a6.mp4 |
| 4 | 暗调箭头 | dark + region_arrow | success | 811KB | 43.3s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/8d58741fc65740fb977ee61acf452be5.mp4 |
| 5 | 暗调光晕 | dark + halo | success | 901KB | 41.8s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/eb17f375ed0d448cbbe073ded0ed298e.mp4 |
| 6 | 朦胧亮点 | annotate_border + dot_motion | success | 731KB | 20.7s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/eb958ace64b3447f87ec6d395e71c041.mp4 |
| 7 | 多层光晕 | glow_border + halo | success | 784KB | 40.2s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/c99f7003ff72466fba9109886475bcd7.mp4 |
| 8 | 聚光景深 | spotlight + depth_of_field | success | 943KB | 56.3s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/8fb19163a6bf4b87b133aafec1737376.mp4 |
| 9 | 暗调四角 | dark + corner_brackets | success | 824KB | 18.2s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/dbde84deb8f241b2b9a7417a8ca6bd1c.mp4 |
| 10 | 金边箭头 | hl_border + region_arrow | success | 712KB | 32.3s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/0b561a6598064f0e995b4602966e6d59.mp4 |
| 11 | 虚化描边 | bg_blur + animated_outline | success | 694KB | 13.6s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/c72e932d97704bcaafb35fc24e1c2571.mp4 |
| 12 | 揭示金边 | seq_reveal + hl_border | success | 727KB | 27.3s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/20d2fc43d5ff468d8f2d28e0964328fa.mp4 |
| 13 | 聚光描边 | spotlight + animated_outline | success | 1022KB | 21.8s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/5c1453b7a64d4012b0133f057c42e92c.mp4 |
| 14 | 聚光四角 | spotlight + corner_brackets | success | 1002KB | 23.0s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/c747fdb12e3449ac9a1c125fb6c3261e.mp4 |
| 15 | 聚光箭头 | spotlight + region_arrow | success | 983KB | 40.9s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/282b41ae461e4a0a9b9f52935b134367.mp4 |
| 16 | 聚光光晕 | spotlight + halo | success | 1024KB | 41.3s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/5c1c8712cf834c2fa0fddd091e8ac993.mp4 |
| 17 | 暗调朦胧 | dark + annotate_border | success | 821KB | 37.2s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/b418675ae81a4a3a92d16a0e98019161.mp4 |
| 18 | 暗调亮点 | dark + dot_motion | success | 835KB | 21.9s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/63b3d524c758403fb5e861ec50a46c79.mp4 |
| 19 | 金边光晕 | hl_border + halo | success | 803KB | 35.0s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/237817c60f444a29b0c9007795e55ca1.mp4 |
| 20 | 描边箭头 | animated_outline + region_arrow | success | 719KB | 18.9s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/c8e1a283a5f8451c99f68d2555f41964.mp4 |
| 21 | 描边四角 | animated_outline + corner_brackets | success | 718KB | 4.7s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/a654c22581e94f1a9fe638d8c2c33967.mp4 |
| 22 | 虚化金边 | bg_blur + hl_border | success | 688KB | 29.0s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/d271d7ccc06e484a8e0b3de0e7efb64d.mp4 |
| 23 | 虚化四角 | bg_blur + corner_brackets | success | 675KB | 12.9s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/e8c01653a205469c8e0803b07bb74f4b.mp4 |
| 24 | 暗调金边箭头 | dark + hl_border + region_arrow | success | 829KB | 47.9s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/96be9916c900412493ce1ec04d2e3616.mp4 |
| 25 | 暗调描边四角 | dark + animated_outline + corner_brackets | success | 854KB | 19.2s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/1c716dc069d54e5b875a9173225a7ce2.mp4 |
| 26 | 聚光金边光晕 | spotlight + hl_border + halo | success | 1057KB | 52.8s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/14977aa6fffc4a5e88b9e535fe462e14.mp4 |
| 27 | 暗调朦胧亮点 | dark + annotate_border + dot_motion | success | 844KB | 38.1s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/5ad7bb628f3b46b09b5864051e8bf101.mp4 |
| 28 | 暗调金边光晕 | dark + hl_border + halo | success | 919KB | 43.9s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/4143460a11db4f55b25cdea5fcb13531.mp4 |
| 29 | 聚光金边箭头 | spotlight + hl_border + region_arrow | success | 1015KB | 43.0s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/fffd33fda77041b2868943841ecc1db4.mp4 |
| 30 | 暗调描边箭头 | dark + animated_outline + region_arrow | success | 842KB | 32.4s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/c3da5ac8574a4832871fdaf41fe3171b.mp4 |
| 31 | 聚光描边四角 | spotlight + animated_outline + corner_brackets | success | 1027KB | 22.4s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/2f94c6c8601244698c255c34808f65bc.mp4 |
| 32 | 虚化描边四角 | bg_blur + animated_outline + corner_brackets | success | 694KB | 12.2s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/42677f00631b4862867b6426a4ef94e4.mp4 |
| 33 | 聚光描边箭头 | spotlight + animated_outline + region_arrow | success | 1014KB | 32.2s | https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/c4c575029a634ad6a0cb82120a17e595.mp4 |

## A组: 双静态动作叠加（StaticOverlay - Double Static）

### 1. 聚光金边

- **类型**: StaticOverlay
- **动作**: spotlight + hl_border
- **状态**: success
- **文件大小**: 1020KB
- **耗时**: 47.7s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/041ecbcf94ae4db09ce3e7808289d2ef.mp4

### 2. 暗调金边

- **类型**: StaticOverlay
- **动作**: dark + hl_border
- **状态**: success
- **文件大小**: 828KB
- **耗时**: 49.9s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/94e56c235c3c46eba09dc1744d74d926.mp4

### 3. 暗调描边

- **类型**: StaticOverlay
- **动作**: dark + animated_outline
- **状态**: success
- **文件大小**: 847KB
- **耗时**: 25.4s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/50d7bf30f217466fb539a3ae62c243a6.mp4

### 4. 暗调箭头

- **类型**: StaticOverlay
- **动作**: dark + region_arrow
- **状态**: success
- **文件大小**: 811KB
- **耗时**: 43.3s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/8d58741fc65740fb977ee61acf452be5.mp4

### 5. 暗调光晕

- **类型**: StaticOverlay
- **动作**: dark + halo
- **状态**: success
- **文件大小**: 901KB
- **耗时**: 41.8s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/eb17f375ed0d448cbbe073ded0ed298e.mp4

### 6. 朦胧亮点

- **类型**: StaticOverlay
- **动作**: annotate_border + dot_motion
- **状态**: success
- **文件大小**: 731KB
- **耗时**: 20.7s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/eb958ace64b3447f87ec6d395e71c041.mp4

### 7. 多层光晕

- **类型**: StaticOverlay
- **动作**: glow_border + halo
- **状态**: success
- **文件大小**: 784KB
- **耗时**: 40.2s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/c99f7003ff72466fba9109886475bcd7.mp4

### 8. 聚光景深

- **类型**: StaticOverlay
- **动作**: spotlight + depth_of_field
- **状态**: success
- **文件大小**: 943KB
- **耗时**: 56.3s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/8fb19163a6bf4b87b133aafec1737376.mp4

### 9. 暗调四角

- **类型**: StaticOverlay
- **动作**: dark + corner_brackets
- **状态**: success
- **文件大小**: 824KB
- **耗时**: 18.2s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/dbde84deb8f241b2b9a7417a8ca6bd1c.mp4

### 10. 金边箭头

- **类型**: StaticOverlay
- **动作**: hl_border + region_arrow
- **状态**: success
- **文件大小**: 712KB
- **耗时**: 32.3s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/0b561a6598064f0e995b4602966e6d59.mp4

### 11. 虚化描边

- **类型**: StaticOverlay
- **动作**: bg_blur + animated_outline
- **状态**: success
- **文件大小**: 694KB
- **耗时**: 13.6s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/c72e932d97704bcaafb35fc24e1c2571.mp4

### 12. 揭示金边

- **类型**: StaticOverlay
- **动作**: seq_reveal + hl_border
- **状态**: success
- **文件大小**: 727KB
- **耗时**: 27.3s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/20d2fc43d5ff468d8f2d28e0964328fa.mp4

### 13. 聚光描边

- **类型**: StaticOverlay
- **动作**: spotlight + animated_outline
- **状态**: success
- **文件大小**: 1022KB
- **耗时**: 21.8s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/5c1453b7a64d4012b0133f057c42e92c.mp4

### 14. 聚光四角

- **类型**: StaticOverlay
- **动作**: spotlight + corner_brackets
- **状态**: success
- **文件大小**: 1002KB
- **耗时**: 23.0s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/c747fdb12e3449ac9a1c125fb6c3261e.mp4

### 15. 聚光箭头

- **类型**: StaticOverlay
- **动作**: spotlight + region_arrow
- **状态**: success
- **文件大小**: 983KB
- **耗时**: 40.9s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/282b41ae461e4a0a9b9f52935b134367.mp4

### 16. 聚光光晕

- **类型**: StaticOverlay
- **动作**: spotlight + halo
- **状态**: success
- **文件大小**: 1024KB
- **耗时**: 41.3s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/5c1c8712cf834c2fa0fddd091e8ac993.mp4

### 17. 暗调朦胧

- **类型**: StaticOverlay
- **动作**: dark + annotate_border
- **状态**: success
- **文件大小**: 821KB
- **耗时**: 37.2s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/b418675ae81a4a3a92d16a0e98019161.mp4

### 18. 暗调亮点

- **类型**: StaticOverlay
- **动作**: dark + dot_motion
- **状态**: success
- **文件大小**: 835KB
- **耗时**: 21.9s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/63b3d524c758403fb5e861ec50a46c79.mp4

### 19. 金边光晕

- **类型**: StaticOverlay
- **动作**: hl_border + halo
- **状态**: success
- **文件大小**: 803KB
- **耗时**: 35.0s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/237817c60f444a29b0c9007795e55ca1.mp4

### 20. 描边箭头

- **类型**: StaticOverlay
- **动作**: animated_outline + region_arrow
- **状态**: success
- **文件大小**: 719KB
- **耗时**: 18.9s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/c8e1a283a5f8451c99f68d2555f41964.mp4

### 21. 描边四角

- **类型**: StaticOverlay
- **动作**: animated_outline + corner_brackets
- **状态**: success
- **文件大小**: 718KB
- **耗时**: 4.7s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/a654c22581e94f1a9fe638d8c2c33967.mp4

### 22. 虚化金边

- **类型**: StaticOverlay
- **动作**: bg_blur + hl_border
- **状态**: success
- **文件大小**: 688KB
- **耗时**: 29.0s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/d271d7ccc06e484a8e0b3de0e7efb64d.mp4

### 23. 虚化四角

- **类型**: StaticOverlay
- **动作**: bg_blur + corner_brackets
- **状态**: success
- **文件大小**: 675KB
- **耗时**: 12.9s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/e8c01653a205469c8e0803b07bb74f4b.mp4

## B组: 三静态动作叠加（StaticOverlay - Triple Static）

### 24. 暗调金边箭头

- **类型**: StaticOverlay
- **动作**: dark + hl_border + region_arrow
- **状态**: success
- **文件大小**: 829KB
- **耗时**: 47.9s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/96be9916c900412493ce1ec04d2e3616.mp4

### 25. 暗调描边四角

- **类型**: StaticOverlay
- **动作**: dark + animated_outline + corner_brackets
- **状态**: success
- **文件大小**: 854KB
- **耗时**: 19.2s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/1c716dc069d54e5b875a9173225a7ce2.mp4

### 26. 聚光金边光晕

- **类型**: StaticOverlay
- **动作**: spotlight + hl_border + halo
- **状态**: success
- **文件大小**: 1057KB
- **耗时**: 52.8s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/14977aa6fffc4a5e88b9e535fe462e14.mp4

### 27. 暗调朦胧亮点

- **类型**: StaticOverlay
- **动作**: dark + annotate_border + dot_motion
- **状态**: success
- **文件大小**: 844KB
- **耗时**: 38.1s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/5ad7bb628f3b46b09b5864051e8bf101.mp4

### 28. 暗调金边光晕

- **类型**: StaticOverlay
- **动作**: dark + hl_border + halo
- **状态**: success
- **文件大小**: 919KB
- **耗时**: 43.9s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/4143460a11db4f55b25cdea5fcb13531.mp4

### 29. 聚光金边箭头

- **类型**: StaticOverlay
- **动作**: spotlight + hl_border + region_arrow
- **状态**: success
- **文件大小**: 1015KB
- **耗时**: 43.0s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/fffd33fda77041b2868943841ecc1db4.mp4

### 30. 暗调描边箭头

- **类型**: StaticOverlay
- **动作**: dark + animated_outline + region_arrow
- **状态**: success
- **文件大小**: 842KB
- **耗时**: 32.4s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/c3da5ac8574a4832871fdaf41fe3171b.mp4

### 31. 聚光描边四角

- **类型**: StaticOverlay
- **动作**: spotlight + animated_outline + corner_brackets
- **状态**: success
- **文件大小**: 1027KB
- **耗时**: 22.4s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/2f94c6c8601244698c255c34808f65bc.mp4

### 32. 虚化描边四角

- **类型**: StaticOverlay
- **动作**: bg_blur + animated_outline + corner_brackets
- **状态**: success
- **文件大小**: 694KB
- **耗时**: 12.2s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/42677f00631b4862867b6426a4ef94e4.mp4

### 33. 聚光描边箭头

- **类型**: StaticOverlay
- **动作**: spotlight + animated_outline + region_arrow
- **状态**: success
- **文件大小**: 1014KB
- **耗时**: 32.2s
- **OSS URL**: https://yufa-polymas.oss-cn-hangzhou.aliyuncs.com/ai/wuzhitong/2026-07-15/c4c575029a634ad6a0cb82120a17e595.mp4
