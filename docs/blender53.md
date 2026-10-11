# Blender 5.3 适配与验证

本分支版本：0.6.0。验证日期：2026-10-11。运行环境：Windows，Blender 5.3.0 Beta `3e084785de9a`，Python 3.13.13。

## 上游依据

核对的 Importer 提交为 [`fb77f982`](https://github.com/ShiyumeMeguri/RuriRipperImporter/commit/fb77f982c97c6e3d72a998782c76c18cae39fabf)，版本号仍是 3.0.0。本次新增提交修复替身材质的来源记录及保存／载入恢复。

其 README 仍写 Blender 5.1+，但[终末地生成着色栈](https://github.com/ShiyumeMeguri/RuriRipperImporter/blob/fb77f982c97c6e3d72a998782c76c18cae39fabf/Game/Endfield/shader/Blender/__init__.py) 已明确要求 5.3。主要实现是数值组输入的批量 RNA 写入、常量折叠、原生逐灯图，以及对象属性传递骨骼基座。Shader stamp 为 `c36a9b36cda22a91`。

[Blender 5.3 官方说明](https://developer.blender.org/docs/release_notes/5.3/eevee/)列出 Light Info、Light Evaluation、Shadow Raycast、Light Accumulation 和 Attribute 的 Light 模式。这些能力已在上述本机二进制中实际创建并渲染验证。Hook 上游仍为 `ed62f85a`，没有新的提交。

## 本分支实现

`native53.py` 独立管理转移后材质的数值输入。它缓存绑定位置，先验证整个编辑，再按节点批量提交；不同着色阶段只读取 RGB 或只读取 W 的同一参数合并为一个完整通道。已有连线／驱动及结构已变化的输入受到保护。由 Importer 管理的源材质仍应在 Ruri 面板调节，避免临时图与源记录不一致。

参数控件、Scene 动画通道、默认值和预设都支持原生材质。复制时保留当前数值与必要的 CMB 元数据；不会让材质重新被 Importer 的临时数据回收流程接管。User 版也包含这条运行路径。

原生灯光图保持原有着色算法和连接，纯原生场景无需脚本灯表上传。仍引用旧灯表的材质保留原路径；混合场景继续更新所需的旧表。原生逐灯图处理场景灯光，旧路径的主日光选择不会被错误地当作原生图的过滤器。

`environment53.py` 只绑定来源可确认的 World 采样器和明确的环境查询口。环境值按场景共用，通过原生属性读取；颜色、强度、Mapping 数值变化不重建每张材质。拓扑／图像变化才重新绑定。支持 Background、Environment Texture、Mapping；复杂 World 图保留上次有效值并提示，不猜测程序化节点的含义。

相机投影和方向每轮计算一次，再变换到各目标空间；数值型接收节点一次提交多个输入。新版骨骼基座使用世界空间，同一骨骼的结果复用；旧材质继续使用对象空间，两种属性名独立。原始对象和本帧求值对象保持一致。

旧参数图写入仍兼容原包，只为修改过的分量保存回滚数据，避免多复制一份整图。已有工程升级后可点击「初始化／修复独立同步」接入适配；不会把所有历史着色图自动重写成新的逐灯算法。

## CPU 性能对比

在同一 Blender 二进制中，以原 `main` 的 0.5.26 源码与本分支分别运行。预热 5 次后测量 50 次，取中位数。

| 测试 | 0.5.26 | 0.6.0 | 说明 |
| --- | ---: | ---: | --- |
| 旧参数图实时写入，不打包 | 0.394 ms | 0.336 ms | 约减少 15% CPU 时间 |
| 原生数值输入写入 | 无该路径 | 0.097 ms | 约为原旧参数图路径的 1/4 |
| 128 个对象的相机参数计算 | 0.441 ms | 0.133 ms | 约减少 70% CPU 时间 |
| 旧参数图写入并打包 | 1.705 ms | 1.670 ms | 打包仍占主要成本，差异较小 |

参数图为 512×512 RGBA 浮点，修改两个参数；原生测试更新八个组实例中的对应值。相机样本的数值另行比对。这里测量 Python／RNA 的 CPU 传递与计算时间，不含完整依赖图、着色器编译、GPU 绘制，不能换算为视口 FPS 或任意角色的整体倍数。

可通过 `dev/benchmark_blender53.py` 重测。给测试运行器追加一个源码根目录参数，可对那个源码版本执行相同基准。

## 功能与回归

- 原生路径：受限上下文注册、批量写入、RGBA/W、无关输入保护、非有限值拒绝、连线保护、参数面板、动画插值、暂停／手动编辑、预设、克隆、停用后持久数据及保存重载。
- 骨骼基座：旋转目标网格不改变原生世界空间基座；旧对象空间路径作为对照，数值区分明确。
- EEVEE 图像：World 颜色／强度、World 节点改名、HDRI 旋转／强度、日光能量／旋转。HDRI 接入前后图像一致，纯原生日光没有脚本灯表上传。
- 真实上游着色栈：Standard、Face、Eyes、Hair、Fur、Eyebrow、VFX、OverlayShadow、LiquidAg 九个部位，共识别 405 个当前有效参数。复制后的数值相等，依赖闭包已本地化；Standard 另完成编辑、停用 Importer 及保存重载验证。
- 旧流程：参数引擎、参数动画渲染、相机变换／镜头设置、暂停／恢复、NLA／驱动、约束、骨骼改名、源包→最终包→接收端→恢复的应用链。
- 构建：User 裁剪、保守匹配、两版 ZIP 一致性、缺少角色包的独立构建，以及两版 ZIP 的真实安装／启停。

自测使用临时偏好设置和工作区，保留日常 Blender 会话。未导入完整真实角色进行全套视觉／动作验收，也未测量实际项目的视口 FPS。原生灯节点为 EEVEE 功能；本分支不宣称获得相同的 Cycles 原生逐灯行为。

## 复现

```shell
python dev/build_release.py --plugin-only --out dist/blender53-0.6.0
python dev/run_blender53_tests.py --blender "<Blender 5.3 路径>/blender.exe" --script dev/test_blender53.py --out test_output/blender53/native
python dev/run_blender53_tests.py --blender "<Blender 5.3 路径>/blender.exe" --script dev/test_blender53_render.py --out test_output/blender53/render
python dev/run_blender53_tests.py --blender "<Blender 5.3 路径>/blender.exe" --script dev/test_blender53_install.py --out test_output/blender53/user dist/blender53-0.6.0/character_material_bridge_user-0.6.0.zip
```

普通使用者安装 User ZIP；材质包作者安装开发版。此分支没有修改 `main`、旧 Release 或旧仓库，没有添加许可证、LFS 或自动发布流程。
