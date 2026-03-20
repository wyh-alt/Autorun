# Autorun

Autorun 是一个基于 PyQt6 的 Windows 桌面启动器，用于统一管理多个程序的启动、停止、顺序运行、日志查看与导出，并提供 CPU 利用率、显存占用和异常状态可视化。

本项目适合以下场景：

- 同时维护多个 Python/可执行程序的日常启动与监控
- 批处理任务需要按顺序、按间隔启动
- 需要长期驻留托盘，随时查看状态与日志
- 需要快速定位运行异常与退出原因

## 核心功能

- 程序管理
  - 添加、编辑、删除程序配置
  - 自定义备注名、启动参数、工作目录、解释器路径
  - 支持拖拽排序及按钮上下移动
- 运行控制
  - 运行选中
  - 停止选中
  - 停止所有（带确认）
  - 运行所有（支持顺序启动间隔）
- 输出与日志
  - 单程序实时终端输出窗口
  - 全局日志窗口与单程序日志窗口
  - 日志导出为 UTF-8 文本
  - 日志事件覆盖启动、终止、退出、异常、启动失败等关键动作
- 状态监控
  - 状态列：未运行 / 运行中 / 异常
  - CPU 利用率
  - 显存占用（MiB）
  - 错误信息列（保留最近异常）
- 托盘能力
  - 关闭窗口时可选择隐藏到托盘或退出
  - 可记住本次关闭动作
  - 托盘图标右上角状态点（绿/黄/红）
- Python 运行体验优化
  - 自动尝试识别虚拟环境解释器（.venv / venv / env / workenv）
  - 运行前提醒未设置解释器的 .py 程序
  - 混合编码输出自适应解码，降低中文乱码概率

## 目录结构

```text
autorun/
├─ main.py
├─ config.json
├─ autorun_app/
│  ├─ main_window.py
│  ├─ process_manager.py
│  ├─ dialogs.py
│  ├─ models.py
│  ├─ storage.py
│  ├─ icon.png
│  └─ icon.ico
├─ autorun_app.spec
└─ README.md
```

## 环境要求

- Windows 10/11（核心功能以 Windows 为主）
- Python 3.10+（建议 3.10 或 3.11）
- 依赖：
  - PyQt6

可选能力：

- NVIDIA 显卡 + 驱动（用于显存占用采集）

## 快速开始

1) 安装依赖

```bash
pip install PyQt6
```

2) 启动程序

```bash
python main.py
```

3) 首次使用建议

- 先添加 1~2 个程序验证路径与参数
- 对 `.py` 程序优先填写虚拟环境解释器路径
- 使用“显示终端”和“日志”观察输出是否正常

## 配置文件说明

配置文件默认位于项目根目录 `config.json`，结构如下：

```json
{
  "programs": [
    {
      "program_id": "uuid",
      "path": "D:/xxx/start.py",
      "name": "任务A",
      "args": "--port 8080",
      "workdir": "D:/xxx",
      "interpreter": "D:/xxx/.venv/Scripts/python.exe"
    }
  ]
}
```

字段说明：

- `program_id`: 程序唯一标识（自动生成）
- `path`: 程序路径（必填）
- `name`: 备注名称（可选）
- `args`: 启动参数（可选）
- `workdir`: 工作目录（可选）
- `interpreter`: 解释器/运行时路径（可选，`.py` 建议填写）

## 解释器自动检测策略

当程序是 `.py/.pyw` 且解释器为空时，系统会在以下位置按顺序查找：

- 工作目录
- 脚本所在目录
- 上述目录向上最多 3 级父目录

候选解释器目录：

- `.venv/Scripts/python.exe`
- `venv/Scripts/python.exe`
- `env/Scripts/python.exe`
- `workenv/Scripts/python.exe`

运行“运行所有”时，如果仍有未设置解释器的 Python 程序，会一次性列出清单并确认是否继续。

## 显存占用说明

显存采集采用分层策略：

1. Windows 优先：通过 GPU 性能计数器按 PID 采集 DedicatedUsage  
2. 进程树聚合：将主进程及其子孙进程显存合并归属到同一任务  
3. 回退策略：无法获得进程级数据时，回退到 `nvidia-smi` 可用信息

注意：

- 不同驱动模式（如 WDDM）对进程级可见性有差异
- 某些图形/混合负载进程可能短时不可见，UI 将显示为 `—`

## 打包发布（目录模式）

项目使用 PyInstaller 打包为目录模式（非单文件）。

```bash
python -m PyInstaller --noconfirm --clean --onedir --windowed --name autorun_app --icon autorun_app/icon.ico --add-data "autorun_app/icon.png;autorun_app" main.py
```

产物目录：

- `dist/autorun_app/autorun_app.exe`

分发时请拷贝整个 `dist/autorun_app` 目录，不要只拷贝 exe。

## 常见问题

### 1. 终端输出中文乱码

- 优先确保目标 Python 程序在 UTF-8 环境下输出
- 启动器已内置多编码自适应解码（UTF-8 / GB18030 / 系统编码）
- 如仍异常，建议在目标程序中显式设置 stdout 编码

### 2. 任务栏图标不更新或显示为 Python 图标

- 确认使用最新打包产物
- 彻底退出程序后重启
- 必要时重启 Windows Explorer 或清理图标缓存

### 3. 多任务显存占用显示不完整

- 检查是否被安全软件拦截性能计数器读取
- 确认 NVIDIA 驱动正常
- 图形进程在某些驱动模式下可能存在可见性限制

## 开发建议

- 修改核心逻辑后先执行：

```bash
python -m compileall .
```

- 如需提交中文 commit message，建议保持 Git 编码为 UTF-8：
  - `i18n.commitEncoding=utf-8`
  - `i18n.logOutputEncoding=utf-8`

## 许可证

当前仓库未声明开源许可证。若计划公开分发，建议补充 `LICENSE` 文件并明确使用条款。
