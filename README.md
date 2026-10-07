# Character Material Bridge 0.5.26（角色材质桥）

Blender 角色材质与修改器移植插件，维护者：**新杨XIYAG**。用于将终末地 Ruri 风格源工程中的材质、节点和外观修改器，匹配并应用到同角色的 MMD 模型；支持调参、参数动画、独立运行同步及最终材质包分发。

本仓库公开插件源码、构建链、基础验证和用户说明。普通使用者请优先下载 User 用户版。

## 下载安装

当前发布：[v0.5.26](https://github.com/X-I-Y-A-G/character-material-bridge-blender/releases/tag/v0.5.26)。

**安装要求：Blender 5.2 及以上版本（5.2+）。插件须在此版本范围内安装、使用和验证。** 已完成运行验收的环境为 Windows / Blender 5.2.2 LTS。

**普通使用者请下载 User 用户版：** [character_material_bridge_user-0.5.26.zip](https://github.com/X-I-Y-A-G/character-material-bridge-blender/releases/download/v0.5.26/character_material_bridge_user-0.5.26.zip)。接收并使用别人提供的材质包，进行材质移植、调参或动画渲染，选择这一版即可。

**开发版供需要制作、导出源材质包或最终材质包的作者使用，普通使用者无需安装。** 两版只能启用其中一版。

| 安装包 | 用途 | 下载 |
| --- | --- | --- |
| **User 用户版（普通用户推荐）** `character_material_bridge_user-0.5.26.zip` | 接收并应用材质包，保留参数调节、动画和运行同步；裁剪源包／最终包导出及开发版专用入口 | [下载 User 用户版](https://github.com/X-I-Y-A-G/character-material-bridge-blender/releases/download/v0.5.26/character_material_bridge_user-0.5.26.zip) |
| 开发版（材质包作者使用）`character_material_bridge-0.5.26.zip` | 完整工具链，包含源材质包及最终材质包导出 | [开发版 ZIP](https://github.com/X-I-Y-A-G/character-material-bridge-blender/releases/download/v0.5.26/character_material_bridge-0.5.26.zip) |
| `SHA256SUMS.txt` | 校验两份安装包的原始文件 | [校验文件](https://github.com/X-I-Y-A-G/character-material-bridge-blender/releases/download/v0.5.26/SHA256SUMS.txt) |

1. 普通使用者下载上方 **User 用户版 ZIP**，在 Blender 偏好设置中从磁盘安装并启用用户版条目。
2. 开发版与用户版可以同时安装，但只能启用其中一版；两版共用操作入口和场景属性。
3. 入口：3D 视图 → N 侧栏 →「材质移植」。接收方选择材质包、选中目标网格、分析并确认匹配，再应用；最终包如有配套 `.mapping.json`，可先载入预设。
4. 升级后建议重启 Blender，以释放旧版已加载模块和回调。完整操作说明见[插件说明](character_material_bridge/README.md)及[用户版说明](dev/README_user.md)。

请下载上表的安装包。Release 自动提供的 **Source code** ZIP 是整个源码仓库，目录结构不等于两份 Blender 安装包。

## 当前能力

- 源材质／修改器导出，保守匹配、人工确认及映射预设，事务化移植、恢复与失败回滚。
- 参数调节、参数预设、关键帧／驱动／NLA 动画，以及独立灯光、相机描边、头骨基座同步。
- 切线修复、描边平滑与宽度控制、Face／Hair 描边适配、多层毛绒生成或复用。
- 源 UV 复制、最终包网格着色校正还原、自定义 OCIO 兼容和内置后处理。

源包自 0.5.20 起保存清理后的 UV 参考网格，最终包可保存着色校正参考网格；当前功能不再等同于早期的“所有网格均为零顶点”。角色材质包不随本源码仓库分发。插件必需的内置后处理资源 `character_material_bridge/assets/endfield_post.blend` 随源码和安装包保留。

## 源码与独立构建

```text
character_material_bridge/   完整开发版源码、参数布局及内置资源
dev/                       构建脚本、用户版变换器、4 项基础验证与用户版说明
README.md                  项目入口
```

`packages/`（本地角色材质包及映射预设）、`dist/`、`test_output/` 和 Python 缓存不进入 Git。历史安装包仍保留在维护者本机，本仓库首次 Release 只提供 0.5.26 两版。

使用 Python 3 在仓库根目录执行，不需要启动 Blender，也不需要安装 `bpy`：

```shell
python dev/build_release.py --plugin-only --out dist
```

输出两份安装包和 `release.json`。`--plugin-only` 不检查或读取角色材质包及其审计文件；清单不包含 `package`、`package_sha256`、`package_audit`。开发版源码直接打包，用户版通过内存中的裁剪变换生成。

原默认模式及 Python 调用 `build(out_dir)` 保持可用。默认模式仍要求本地的 `packages/jue_A_无模型材质包_v0.5.blend` 和 `dev/package_audit_v05pkg.json`（两者均不随源码仓库提供），并把示例包元数据写入清单：

```shell
python dev/build_release.py --out dist
```

Python API 的独立构建入口为 `build(out_dir, plugin_only=True)`。`.gitattributes` 保留插件目录与用户版 README 的原始字节，使 Windows／Linux 检出不会因行尾转换改变安装包内容。

## 验证

纯 Python 发布检查：

```shell
python dev/test_user_transform.py
python dev/test_match_v2.py
python dev/test_dev_zip_parity.py
python dev/test_plugin_only_build.py
```

发布一致性测试使用自动清理的唯一临时目录；可通过 `CMB_TEST_OUT` 指定临时目录的父目录。本地示例资产齐全时，还会检查默认模式与独立模式生成的 ZIP 一致。独立构建测试另行验证缺少角色材质包和审计文件的干净副本。

**运行验证边界：** 本插件要求在 **Blender 5.2 及以上版本（5.2+）** 安装、使用和验证。已有运行验收环境为 **Windows / Blender 5.2.2 LTS**，其他版本需在实际环境中验证。用户反馈的 Blender 5.3 Alpha 原始崩溃尚未复现；已有修复清理了 GUI 渲染工作线程中的上下文覆盖、递归依赖图更新及 GPU 释放路径。本次公开前另在 Windows / Blender 5.2.1 LTS 隔离配置中验证了两版 ZIP 的安装、启用／停用及内置资源；完整渲染与视觉验收仍沿用既有记录。
