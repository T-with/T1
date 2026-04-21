"""
hotfix.py — 修复 Phase 2/3 应用阶段的 Windows/依赖问题

执行顺序:
 1. 覆盖 phase2/apply_phase2.py, phase3/apply_phase3.py, phase3/apply_phase3_instrumentation.py
    (去掉 emoji + 加 utf-8 header)
 2. 覆盖 tests/test_phase2.py (修 importlib.reload bug)
 3. 追加 structlog + prometheus-client 到根目录 requirements.txt (如果还没)
 4. 本地 pip install 新依赖
 5. 重新跑 apply_phase2, apply_phase3, apply_phase3_instrumentation
 6. 提示你重建 docker image

用法:
    先把这个 hotfix 包解压到项目根,然后:
    python hotfix.py
"""

import sys
import io
if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
HOTFIX = ROOT / 'hotfix_files'

# 预检查
if not (ROOT / 'app.py').exists():
    print("[X] 不在项目根目录"); sys.exit(1)
if not HOTFIX.exists():
    print(f"[X] 缺 hotfix_files/ 目录: {HOTFIX}"); sys.exit(1)
if not (ROOT / 'phase2').exists() or not (ROOT / 'phase3').exists():
    print("[X] phase2/ 或 phase3/ 目录不存在,先跑 install_phase2_3.py")
    sys.exit(1)

print(f"[OK] 项目根目录: {ROOT}\n")

# 1. 覆盖 apply 脚本
print("[1/5] 覆盖 apply 脚本 (去 emoji, 加 utf-8 header)")
mapping = [
    (HOTFIX / 'apply_phase2.py', ROOT / 'phase2' / 'apply_phase2.py'),
    (HOTFIX / 'apply_phase3.py', ROOT / 'phase3' / 'apply_phase3.py'),
    (HOTFIX / 'apply_phase3_instrumentation.py', ROOT / 'phase3' / 'apply_phase3_instrumentation.py'),
]
for src, dst in mapping:
    shutil.copy2(src, dst)
    print(f"  [v] {dst.relative_to(ROOT)}")

# 2. 覆盖 test_phase2.py
print("\n[2/5] 修复 tests/test_phase2.py")
shutil.copy2(HOTFIX / 'test_phase2.py', ROOT / 'tests' / 'test_phase2.py')
print("  [v] tests/test_phase2.py")

# 3. 追加依赖到 requirements.txt
print("\n[3/5] 追加 structlog + prometheus-client 到 requirements.txt")
req = ROOT / 'requirements.txt'
text = req.read_text(encoding='utf-8')
added = []
if 'structlog' not in text:
    text += '\nstructlog>=24.0\n'
    added.append('structlog')
if 'prometheus' not in text:
    text += 'prometheus-client>=0.20\n'
    added.append('prometheus-client')
if added:
    req.write_text(text, encoding='utf-8')
    print(f"  [v] 追加: {', '.join(added)}")
else:
    print("  [SKIP] 已存在")

# 4. pip install
print("\n[4/5] 本地安装新依赖 (本地跑测试用)")
r = subprocess.run([sys.executable, '-m', 'pip', 'install', 'structlog>=24.0', 'prometheus-client>=0.20'],
                   capture_output=True, text=True)
# 只打印最后几行,避免太吵
tail = (r.stdout + r.stderr).strip().split('\n')[-5:]
for line in tail:
    print(f"  {line}")
if r.returncode != 0:
    print("  [!] pip 失败,但不致命 — Docker 里会再装一次")

# 5. 重新跑 apply 脚本
print("\n[5/5] 重新应用 Phase 2 + Phase 3 到 app.py")
for script in ['phase2/apply_phase2.py',
               'phase3/apply_phase3.py',
               'phase3/apply_phase3_instrumentation.py']:
    print(f"\n--- 跑 {script} ---")
    # 用 utf-8 环境变量
    env = dict(__import__('os').environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    r = subprocess.run([sys.executable, str(ROOT / script)],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding='utf-8', env=env)
    print(r.stdout)
    if r.returncode != 0:
        print(r.stderr)
        print(f"  [!] {script} 失败")

print("\n" + "=" * 60)
print("[OK] hotfix 执行完成")
print()
print("下一步:")
print()
print("  A. 跑本地测试验证 (应全绿):")
print("     python -m pytest tests/ -v")
print()
print("  B. 重建 Docker 镜像 (让 structlog + prometheus-client 进容器):")
print("     docker compose -f docker-compose.yml -f docker-compose.monitoring.yml down")
print("     docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d --build")
print()
print("  C. 验证 /metrics 返回真实数据 (不是空 25 字节):")
print('     curl.exe http://localhost:8080/metrics | Select-String "strategies_running"')
print("=" * 60)
