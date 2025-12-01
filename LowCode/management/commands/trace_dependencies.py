# trace_sql_template_deps.py
# 多目标模块追踪：允许同时指定多个目标模块，如 sql_template 和 query_builder。
# 导出为 JSON 报告：除了生成调用关系图外，还将调用链和相关数据保存为 JSON 文件，方便CI集成使用。
# 集成到 Django 管理命令：将脚本的功能封装成一个Django管理命令，可以直接通过 manage.py 来运行。
# 在Django项目的根目录下执行以下命令来追踪多个目标模块并生成相应的输出：
# Shell
# 编辑
# python manage.py trace_dependencies --targets sql_template query_builder
# 此命令会自动分析项目，生成调用关系图以及JSON格式的调用链报告。JSON报告可以帮助你在持续集成(CI)环境中自动化地检查模块间的依赖关系。
# lowcode/management/commands/trace_dependencies.py

import os
import sys
import ast
import json
from pathlib import Path
from collections import defaultdict, deque
from typing import Dict, Set, List, Optional

from django.core.management.base import BaseCommand


# ======================
# 配置：要忽略的目录
# ======================
IGNORE_DIRS = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "env",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
    "migrations",
    "tests",
    "test",
    "static",
    "media",
}


# ======================
# 工具函数
# ======================
def is_ignored(path: Path) -> bool:
    """判断路径是否应被忽略"""
    return any(part in IGNORE_DIRS for part in path.parts)


# ======================
# AST 解析器：构建函数调用图
# ======================
class CallGraphVisitor(ast.NodeVisitor):
    def __init__(self, file_path: Path, project_root: Path):
        self.file_path = file_path
        self.project_root = project_root
        self.module_name = self._get_module_name(file_path)
        self.functions: Dict[str, List[str]] = {}  # func_qualified_name -> [called_names]
        self.imports: Dict[str, str] = {}          # local_name -> full.qualified.name
        self.current_function: Optional[str] = None

    def _get_module_name(self, path: Path) -> str:
        """将文件路径转换为 Python 模块名（基于项目根目录）"""
        try:
            rel = path.relative_to(self.project_root)
            parts = list(rel.parts)
            if parts[-1] == "__init__.py":
                parts = parts[:-1]
            elif parts[-1].endswith(".py"):
                parts[-1] = parts[-1][:-3]
            return ".".join(parts)
        except ValueError:
            # 如果不在 project_root 下，回退到文件名
            return path.stem

    def visit_Import(self, node):
        for alias in node.names:
            name = alias.name
            asname = alias.asname or alias.name.split(".")[-1]
            self.imports[asname] = name
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module:
            for alias in node.names:
                full_name = f"{node.module}.{alias.name}"
                asname = alias.asname or alias.name
                self.imports[asname] = full_name
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        func_name = f"{self.module_name}.{node.name}"
        self.current_function = func_name
        self.functions[func_name] = []
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self.visit_FunctionDef(node)

    def visit_Call(self, node):
        if self.current_function is None:
            return
        qualified_name = self._resolve_call(node.func)
        if qualified_name:
            self.functions[self.current_function].append(qualified_name)
        self.generic_visit(node)

    def _resolve_call(self, func_node) -> Optional[str]:
        """将 AST 调用节点解析为 qualified name（如 'sql_template.render'）"""
        if isinstance(func_node, ast.Name):
            name = func_node.id
            if name in self.imports:
                return self.imports[name]
            else:
                # 假设是当前模块内的函数
                return f"{self.module_name}.{name}"
        elif isinstance(func_node, ast.Attribute):
            value = func_node.value
            attr = func_node.attr
            if isinstance(value, ast.Name):
                base = value.id
                if base in self.imports:
                    return f"{self.imports[base]}.{attr}"
                else:
                    return f"{self.module_name}.{base}.{attr}"
        # 更复杂的调用（如 a().b()）暂不支持，但不影响主要场景
        return None


# ======================
# 核心分析逻辑
# ======================
def build_project_call_graph(project_root: Path) -> Dict[str, List[str]]:
    """构建整个项目的函数调用图（qualified_name -> [called_functions]）"""
    call_graph = defaultdict(list)
    for py_file in project_root.rglob("*.py"):
        if is_ignored(py_file):
            continue
        try:
            with open(py_file, "r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source, filename=str(py_file))
            visitor = CallGraphVisitor(py_file, project_root)
            visitor.visit(tree)
            for func, calls in visitor.functions.items():
                call_graph[func].extend(calls)
        except (SyntaxError, UnicodeDecodeError, OSError) as e:
            print(f"⚠️ 跳过文件 {py_file}: {e}", file=sys.stderr)
    return dict(call_graph)


def find_all_callers(call_graph: Dict[str, List[str]], targets: Set[str]) -> Dict[str, Set[str]]:
    """反向查找所有直接或间接调用每个 target 的函数"""
    reverse_graph = defaultdict(list)
    for caller, callees in call_graph.items():
        for callee in callees:
            reverse_graph[callee].append(caller)

    result = {}
    for target in targets:
        visited = set()
        queue = deque([target])
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            for caller in reverse_graph.get(current, []):
                if caller not in visited:
                    queue.append(caller)
        result[target] = visited - {target}  # 排除自身
    return result


def extract_call_chains(
    call_graph: Dict[str, List[str]],
    callers: Dict[str, Set[str]],
    targets: Set[str]
) -> Dict[str, List[List[str]]]:
    """为每个 target 提取从调用者到目标的完整调用链（BFS 找一条路径）"""
    chains = {target: [] for target in targets}
    for target in targets:
        for start in callers[target]:
            queue = deque([[start]])
            found = False
            seen_paths = set()
            while queue and not found:
                path = queue.popleft()
                last = path[-1]
                path_key = tuple(path)
                if path_key in seen_paths:
                    continue
                seen_paths.add(path_key)
                for next_func in call_graph.get(last, []):
                    new_path = path + [next_func]
                    if next_func == target:
                        chains[target].append(new_path)
                        found = True
                        break
                    if next_func not in path and len(new_path) < 20:  # 防止无限递归
                        queue.append(new_path)
            if not found:
                chains[target].append([start, "...", target])
    return chains


def generate_dot_graph(
    call_graph: Dict[str, List[str]],
    relevant_nodes: Set[str],
    output_file: str,
    targets: Set[str]
):
    """生成 Graphviz .dot 文件"""
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("digraph SqlTemplateCallGraph {\n")
        f.write('    rankdir=LR;\n')
        f.write('    node [shape=box, style=filled, fillcolor="#ffffff"];\n')
        for target in targets:
            f.write(f'    "{target}" [fillcolor="#ffcccc", shape=ellipse];\n')

        added_edges = set()
        for caller, callees in call_graph.items():
            if caller not in relevant_nodes:
                continue
            for callee in callees:
                if callee in relevant_nodes or callee in targets:
                    edge = (caller, callee)
                    if edge not in added_edges:
                        color = "red" if callee in targets else "black"
                        f.write(f'    "{caller}" -> "{callee}" [color={color}];\n')
                        added_edges.add(edge)
        f.write("}\n")


def save_json_report(chains_of_targets: Dict[str, List[List[str]]], report_file: str):
    """保存调用链为 JSON 格式（供 CI 使用）"""
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(chains_of_targets, f, ensure_ascii=False, indent=4)


# ======================
# Django 管理命令
# ======================
class Command(BaseCommand):
    help = "追踪多个目标模块的跨文件调用链，并生成 JSON 报告和调用关系图"

    def add_arguments(self, parser):
        parser.add_argument(
            "--targets",
            nargs="+",
            default=["sql_template"],
            help="要追踪的目标模块名，例如: --targets sql_template query_builder",
        )
        parser.add_argument(
            "--output-dir",
            type=str,
            default=".",
            help="输出目录（默认当前目录）",
        )

    def handle(self, *args, **options):
        output_dir = Path(options["output_dir"]).resolve()
        targets = set(options["targets"])

        if not output_dir.exists():
            self.stdout.write(self.style.ERROR(f"❌ 输出目录不存在: {output_dir}"))
            sys.exit(1)

        self.stdout.write(f"🔍 扫描项目根目录: {output_dir}")
        self.stdout.write(f"🎯 追踪目标模块: {', '.join(targets)}")

        # 构建调用图
        call_graph = build_project_call_graph(output_dir)

        # 查找所有调用者（直接 + 间接）
        all_callers = find_all_callers(call_graph, targets)

        total_calls = sum(len(v) for v in all_callers.values())
        if total_calls == 0:
            self.stdout.write(self.style.SUCCESS("✅ 未发现任何对目标模块的调用。"))
            return

        # 提取调用链
        chains_of_targets = extract_call_chains(call_graph, all_callers, targets)

        # 控制台输出
        for target, chains in chains_of_targets.items():
            self.stdout.write(f"\n📌 对 '{target}' 的调用链（共 {len(chains)} 条）:")
            self.stdout.write("-" * 60)
            for chain in sorted(chains, key=len):
                self.stdout.write(" → ".join(chain))

        # 生成 .dot 图
        relevant_nodes = set()
        for s in all_callers.values():
            relevant_nodes.update(s)
        dot_file = output_dir / "call_graph.dot"
        generate_dot_graph(call_graph, relevant_nodes, str(dot_file), targets)
        self.stdout.write(self.style.SUCCESS(f"✅ 调用图已保存至: {dot_file}"))

        # 生成 JSON 报告
        json_file = output_dir / "call_chains_report.json"
        save_json_report(chains_of_targets, str(json_file))
        self.stdout.write(self.style.SUCCESS(f"✅ JSON 报告已保存至: {json_file}"))

        self.stdout.write(self.style.SUCCESS("\n💡 提示: 使用以下命令生成图片:"))
        self.stdout.write(f"    dot -Tpng call_graph.dot -o call_graph.png")