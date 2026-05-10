"""
============================================
  EC_project 一键打包脚本 (Windows)
  用法: python build_all.py
  产物: dist/ 目录下三个 exe
============================================
"""

import subprocess
import sys
import os

# 确保在项目根目录运行
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
os.chdir(_PROJECT_ROOT)

# 先安装 PyInstaller
def ensure_pyinstaller():
    try:
        import PyInstaller
        print(f"[OK] PyInstaller {PyInstaller.__version__} 已安装")
    except ImportError:
        print("[*] 正在安装 PyInstaller ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller>=6.0"])

BUILD_ORDER = [
    ("Server Manager (后台管理服务)",   "build/build_spec_sm.py"),
    ("Server_economic (子服务节点)",    "build/build_spec_se.py"),
    ("Client (交易终端客户端)",          "build/build_spec_client.py"),
]

def build_one(name, spec_file):
    print(f"\n{'='*60}")
    print(f"  正在打包: {name}")
    print(f"  Spec: {spec_file}")
    print(f"{'='*60}")
    result = subprocess.run(
        [sys.executable, spec_file],
        cwd=_PROJECT_ROOT,
    )
    if result.returncode != 0:
        print(f"\n[FAIL] {name} 打包失败，返回码: {result.returncode}")
        return False
    print(f"[OK] {name} 打包完成")
    return True


def main():
    print("=" * 60)
    print("  EC_project Windows 打包工具")
    print("  项目根目录:", _PROJECT_ROOT)
    print("=" * 60)

    ensure_pyinstaller()

    # 安装各组件依赖到当前 Python 环境（PyInstaller 需要找到它们）
    print("\n[*] 检查/安装组件依赖 ...")
    deps = [
        "Server_manager/requirements.txt",
        "Server_economic/requirements.txt",
        "Client/requirements.txt",
    ]
    for req_file in deps:
        path = os.path.join(_PROJECT_ROOT, req_file)
        if os.path.exists(path):
            print(f"  → 安装: {req_file}")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", path],
                cwd=_PROJECT_ROOT,
            )

    results = []
    for name, spec in BUILD_ORDER:
        ok = build_one(name, spec)
        results.append((name, ok))

    # 汇总
    print(f"\n{'='*60}")
    print("  打包结果汇总")
    print(f"{'='*60}")
    all_ok = True
    for name, ok in results:
        status = "✓ 成功" if ok else "✗ 失败"
        print(f"  {status}  {name}")
        if not ok:
            all_ok = False

    dist_dir = os.path.join(_PROJECT_ROOT, "dist")
    if all_ok:
        print(f"\n[全部成功] 产物位于: {dist_dir}/")
        print("  - ServerManager.exe   (双击启动管理服务)")
        print("  - SE_Node.exe         (双击启动子服务)")
        print("  - TradingTerminal.exe (双击启动交易终端)")
    else:
        print(f"\n[部分失败] 请检查上方错误信息后重试")
        sys.exit(1)


if __name__ == "__main__":
    main()
