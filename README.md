# Character Material Bridge 0.6.1 · Blender 5.3 适配分支

角色材质桥，维护者：**新杨XIYAG**。把 Ruri 风格角色的材质、节点和外观修改器移植到同角色模型，支持参数编辑、动画、独立同步与材质包分发。

本分支为 `codex/blender-5.3`，要求 **Blender 5.3 及以上**。当前实测环境：Windows / Blender **5.3.0 Beta，3e084785de9a**。`main` 和已有 v0.5.26 Release 保持原状。

## 普通用户安装 User 版

普通使用者选择 **`character_material_bridge_user-0.6.1.zip`**。它包含材质移植、参数编辑、动画与实时同步。只有需要制作／导出材质包的作者，才选择开发版 `character_material_bridge-0.6.1.zip`。两版只能启用其中一版。

本分支尚未创建新的 GitHub Release；源码可按下面的命令生成两版 ZIP。仓库 [Releases](https://github.com/X-I-Y-A-G/character-material-bridge-blender/releases) 中的 v0.5.26 是原稳定版，不包含本分支改动。GitHub 的 Source code ZIP 是源码，不能当作插件安装包。

在 Blender 偏好设置中从磁盘安装 ZIP，启用后进入 3D 视图 → N 侧栏 →「材质移植」。升级前保存工程，安装后完整重启 Blender。

## 5.3 实现

- **原生参数**：识别新 Ruri 材质的数值组输入，按节点批量写入 `foreach_set`；只更新发生变化的组，保护已有连线和驱动。参数编辑、默认值、预设、关键帧、驱动与 NLA 使用同一条传递路径。
- **原生灯光**：复制并保留 5.3 Light Info / Light Evaluation / Shadow Raycast / Light Accumulation 图。只有旧图仍需要脚本灯表；纯原生图的灯光变化由 EEVEE 处理。原生路径跟随场景中的灯光，旧路径保留主日光选择规则。
- **环境控制**：「独立运行同步」面板新增「启用世界环境控制」，默认开启，角色环境光跟随 World；关闭后用独立 RGB 与强度控制，可插入关键帧，不修改场景背景。两种模式共用场景参数，数值变化无需逐材质重建。World 模式支持 Background、Environment Texture、Mapping；复杂 World 图保留上次有效值并提示。
- **相机与动画**：同一轮同步只计算一次相机投影，批量更新数值输入；动画求值按实际涉及的通道索引读取。
- **旧包兼容**：旧参数图和灯表仍按原着色方式运行，参数写入只保存被修改的分量用于回滚，减少整图复制。不会把旧着色算法强行替换成新的逐灯算法。

原生快速路径需要材质本身使用新式节点输入／灯光图。安装插件不会自动把所有历史材质包改写为新版着色栈。功能说明、测试边界和性能测量见 [Blender 5.3 适配说明](docs/blender53.md)。

## 材质包

选择目标网格 → 选择 `.blend` 材质包 → 分析匹配 → 核对或载入 `.mapping.json` → 应用。插件处理角色副本，并保留恢复入口。匹配不明确时需人工确认。

角色材质包、测试工程与历史 ZIP 不进入 Git。源包可包含清理后的 UV 参考网格，最终包可包含着色校正参考网格；它们不携带源骨架、权重、形态键或动作。必需的 `assets/endfield_post.blend` 随安装包保留。

完整操作说明：[开发版](character_material_bridge/README.md)、[User 用户版](dev/README_user.md)。

## 构建与验证

```shell
python dev/build_release.py --plugin-only --out dist/blender53-0.6.1
python dev/test_user_transform.py
python dev/test_match_v2.py
python dev/test_dev_zip_parity.py
python dev/test_plugin_only_build.py
```

独立构建不需要角色材质包、审计文件或 Blender。默认 `build(out_dir)` 模式继续支持本地示例包；接口 `build(out_dir, plugin_only=True)` 跳过示例资源。开发版字节直接打包，用户版在内存中裁剪，`.gitattributes` 保持发行输入字节一致。

Blender 自测使用独立临时配置，不读取日常偏好设置：

```shell
python dev/run_blender53_tests.py --blender "<Blender 5.3 路径>/blender.exe" --script dev/test_blender53.py --out test_output/blender53/native
python dev/run_blender53_tests.py --blender "<Blender 5.3 路径>/blender.exe" --script dev/test_blender53_render.py --out test_output/blender53/render
python dev/run_blender53_tests.py --blender "<Blender 5.3 路径>/blender.exe" --script dev/benchmark_blender53.py --out test_output/blender53/benchmark
```

运行检查覆盖原生参数、图像结果、保存重载，以及旧包／旧动画流程。基准报告的是 CPU 同步耗时，不是视口 FPS；未据此宣称所有角色与游戏画面已经完成视觉验收。
