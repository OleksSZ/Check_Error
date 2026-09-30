#!/usr/bin/env python3
"""
oop_checker.py

Статический анализатор Python-кода:
 - Проверяет базовые принципы ООП (инкапсуляция, размер класса, self-usage,
   наследование, отсутствующие/"неполные" атрибуты и т.д.)
 - Ищет потенциальные логические баги, которые обычный запуск в терминале
   не покажет (mutable default args, bare except, сравнение с None через ==,
   сравнение float через ==, неиспользуемые импорты, потенциально
   бесконечные циклы, неконсистентные return и т.д.)
 - Поддерживает проверку сразу нескольких файлов
 - Поддерживает проверку целого проекта (директории со структурой пакетов)
 - Пишет результат в checks.log

Использование:
    python oop_checker.py file1.py file2.py
    python oop_checker.py ./my_project/
    python oop_checker.py ./my_project/ file1.py --output my_report.log
"""

import ast
import sys
import os
import json
import argparse
import builtins
from dataclasses import dataclass
from typing import List

BUILTIN_NAMES = set(dir(builtins))

# Границы "своей" области видимости: при обходе тела функции не заходим
# внутрь вложенных функций/классов - у них будет отдельный обход.
_SCOPE_BOUNDARY = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def iter_own_scope(node):
    """Обходит поддерево узла, не спускаясь в тела вложенных функций/классов."""
    def _walk(n, top):
        for child in ast.iter_child_nodes(n):
            if not top and isinstance(child, _SCOPE_BOUNDARY):
                continue
            yield child
            yield from _walk(child, top=False)
    yield from _walk(node, top=True)


@dataclass
class Issue:
    file: str
    line: int
    severity: str  # "BUG" | "OOP" | "STYLE"
    code: str
    message: str

    def format(self) -> str:
        return f"[{self.severity:5}] {self.file}:{self.line:<5} {self.code:<10} {self.message}"

    def as_dict(self):
        return {"file": self.file, "line": self.line, "severity": self.severity,
                "code": self.code, "message": self.message}


@dataclass
class ClassInfo:
    """Информация об одном классе для межфайлового анализа наследования."""
    name: str
    file: str
    lineno: int
    bases: list       # имена базовых классов (строки), как они написаны в коде
    methods: set
    assigned_attrs: set
    has_init: bool


def _base_names(node: ast.ClassDef):
    names = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            names.append(b.id)
        elif isinstance(b, ast.Attribute):
            names.append(b.attr)  # module.Class -> берём только "Class"
    return names


def collect_class_infos(tree: ast.Module, filename: str):
    """Проход 1: находит все классы файла и их сырую информацию (без наследования)."""
    infos = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        method_names = {m.name for m in methods}
        assigned_attrs = set()
        for m in methods:
            for n in ast.walk(m):
                if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                        and n.value.id == "self" and isinstance(n.ctx, ast.Store)):
                    assigned_attrs.add(n.attr)
        infos.append(ClassInfo(
            name=node.name, file=filename, lineno=node.lineno,
            bases=_base_names(node), methods=method_names,
            assigned_attrs=assigned_attrs, has_init="__init__" in method_names,
        ))
    return infos


def build_registry(all_infos):
    registry = {}
    for info in all_infos:
        registry.setdefault(info.name, info)  # первое имя побеждает при коллизии
    return registry


def resolve_ancestors(info: ClassInfo, registry: dict, visited=None):
    """
    Проход 2: рекурсивно поднимается по цепочке наследования через реестр.
    Возвращает (fully_known, merged_attrs, merged_methods):
      fully_known=False, если хотя бы один предок не найден в проекте
      (внешняя библиотека) - в этом случае доверять "объединённым" данным нельзя.
    """
    visited = visited or set()
    if info.name in visited:
        return True, set(), set()  # защита от циклов в объявлении (A(B) + B(A))
    visited.add(info.name)

    merged_attrs = set(info.assigned_attrs)
    merged_methods = set(info.methods)
    fully_known = True

    for base_name in info.bases:
        if base_name == "object":
            continue
        base_info = registry.get(base_name)
        if base_info is None:
            fully_known = False  # родитель вне проекта - дальше не доверяем
            continue
        sub_known, sub_attrs, sub_methods = resolve_ancestors(base_info, registry, visited)
        fully_known = fully_known and sub_known
        merged_attrs |= sub_attrs
        merged_methods |= sub_methods

    return fully_known, merged_attrs, merged_methods


class FileChecker(ast.NodeVisitor):
    def __init__(self, filename: str, source: str, registry=None, max_complexity: int = 10):
        self.filename = filename
        self.source_lines = source.splitlines()
        self.issues: List[Issue] = []
        self.imported_names = {}   # name -> lineno
        self.used_names = set()
        self.registry = registry or {}       # project-wide class registry (для cross-file анализа)
        self.max_complexity = max_complexity

    def add(self, node, severity, code, message):
        self.issues.append(Issue(self.filename, getattr(node, "lineno", 0), severity, code, message))

    # ---------- отслеживание использования имён (для unused-import) ----------
    def visit_Name(self, node):
        self.used_names.add(node.id)
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            self.imported_names[name] = node.lineno
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.name == "*":
                continue
            name = alias.asname or alias.name
            self.imported_names[name] = node.lineno
        self.generic_visit(node)

    # ---------- функции ----------
    def visit_FunctionDef(self, node):
        self._check_mutable_defaults(node)
        self._check_return_consistency(node)
        self._check_too_many_args(node)
        self._check_unused_locals(node)
        self._check_complexity(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _check_mutable_defaults(self, node):
        for default in list(node.args.defaults) + list(node.args.kw_defaults):
            if default is None:
                continue
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self.add(node, "BUG", "MUT-DEF",
                         f"Функция '{node.name}': изменяемое значение по умолчанию "
                         f"({type(default).__name__}) — общее состояние между вызовами функции.")

    def _check_return_consistency(self, node):
        returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
        has_value = any(r.value is not None for r in returns)
        has_empty = any(r.value is None for r in returns)
        if has_value and has_empty:
            self.add(node, "BUG", "RET-MIX",
                     f"Функция '{node.name}': в одних return есть значение, в других — нет. "
                     f"Возможна неявная логическая ошибка (случайный None).")

    def _check_too_many_args(self, node):
        args = node.args.args
        count = len(args)
        if args and args[0].arg in ("self", "cls"):
            count -= 1
        if count > 6:
            self.add(node, "OOP", "TOO-MANY-ARGS",
                     f"Функция/метод '{node.name}': {count} параметров — слишком много, "
                     f"сложно вызывать и тестировать. Рассмотри группировку в объект/dataclass.")

    def _check_unused_locals(self, node):
        assigned = {}
        read = set()
        for n in iter_own_scope(node):
            if isinstance(n, ast.Name):
                if isinstance(n.ctx, ast.Store):
                    if not n.id.startswith("_"):
                        assigned.setdefault(n.id, n.lineno)
                elif isinstance(n.ctx, ast.Load):
                    read.add(n.id)
        for name, lineno in assigned.items():
            if name not in read:
                self.issues.append(Issue(self.filename, lineno, "BUG", "UNUSED-VAR",
                                          f"В '{node.name}': переменная '{name}' присваивается, но "
                                          f"нигде дальше не читается — вероятно, забытая логика или опечатка в имени."))

    def _check_complexity(self, node):
        """
        Цикломатическая сложность (McCabe): 1 (базовый путь через функцию) +
        1 за каждую точку ветвления (if/elif, for, while, except, and/or,
        тернарник, условие в comprehension, case в match).
        Вложенные функции/классы считаются отдельно, в общий счёт не идут.
        """
        complexity = 1
        for n in iter_own_scope(node):
            if isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(n, ast.IfExp):
                complexity += 1
            elif isinstance(n, ast.BoolOp):
                complexity += len(n.values) - 1
            elif isinstance(n, ast.comprehension):
                complexity += 1 + len(n.ifs)
            elif n.__class__.__name__ == "Match":
                pass  # сами case ниже
            elif n.__class__.__name__ == "match_case":
                # исключаем "case _:" (wildcard, не считается веткой-решением)
                pattern = getattr(n, "pattern", None)
                if not (pattern is not None and pattern.__class__.__name__ == "MatchAs" and pattern.pattern is None):
                    complexity += 1

        if complexity > self.max_complexity:
            self.add(node, "BUG", "HIGH-COMPLEXITY",
                     f"Функция '{node.name}': цикломатическая сложность {complexity} "
                     f"(порог {self.max_complexity}) — слишком много независимых путей выполнения, "
                     f"сложно покрыть тестами и легко пропустить баг в редкой ветке.")

    # ---------- классы / ООП ----------
    def visit_ClassDef(self, node):
        self._check_class(node)
        self.generic_visit(node)

    def _check_class(self, node: ast.ClassDef):
        methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        method_names = {m.name for m in methods}
        has_init = "__init__" in method_names

        # 1. Именование класса (PascalCase)
        if not node.name[:1].isupper() or "_" in node.name:
            self.add(node, "OOP", "NAMING",
                     f"Класс '{node.name}': имя класса принято писать в PascalCase (CapWords).")

        # 2. God-class эвристика (нарушение Single Responsibility)
        if len(methods) > 15:
            self.add(node, "OOP", "GOD-CLASS",
                     f"Класс '{node.name}': {len(methods)} методов — вероятно, класс делает "
                     f"слишком много (нарушение Single Responsibility Principle).")

        # 3. self.attr: читается vs присваивается (энкапсуляция / потенциальный AttributeError).
        # Учитываем атрибуты, унаследованные из родителей по всему проекту (см. resolve_ancestors).
        assigned_attrs, read_attrs = set(), set()
        for m in methods:
            for n in ast.walk(m):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self":
                    if isinstance(n.ctx, ast.Store):
                        assigned_attrs.add(n.attr)
                    elif isinstance(n.ctx, ast.Load):
                        read_attrs.add(n.attr)

        inherited_known, inherited_attrs, inherited_methods = True, set(), set()
        my_info = self.registry.get(node.name)
        if my_info is not None and my_info.bases:
            inherited_known, inherited_attrs, inherited_methods = resolve_ancestors(my_info, self.registry)

        if inherited_known:  # если хоть один родитель "не виден" - не рискуем ложным срабатыванием
            missing = read_attrs - assigned_attrs - method_names - inherited_attrs - inherited_methods
            for attr in sorted(missing):
                self.add(node, "BUG", "ATTR-MISS",
                         f"Класс '{node.name}': 'self.{attr}' читается, но нигде в классе (и его "
                         f"известных родителях по проекту) явно не присваивается — возможен "
                         f"AttributeError в рантайме.")

        if not has_init and assigned_attrs:
            self.add(node, "OOP", "NO-INIT",
                     f"Класс '{node.name}': атрибуты создаются вне __init__ — объект может "
                     f"оказаться в неполном/непредсказуемом состоянии в зависимости от порядка вызовов.")

        # 4. Метод не использует self -> кандидат в @staticmethod
        for m in methods:
            if m.name in ("__init__", "__new__"):
                continue
            decorators = {self._decorator_name(d) for d in m.decorator_list}
            if "staticmethod" in decorators or "classmethod" in decorators:
                continue
            if not m.args.args or m.args.args[0].arg != "self":
                continue
            uses_self = any(isinstance(n, ast.Name) and n.id == "self" for n in ast.walk(m))
            if not uses_self:
                self.add(m, "OOP", "NO-SELF",
                         f"Метод '{node.name}.{m.name}': не использует 'self' — вероятно, "
                         f"стоит сделать его @staticmethod.")

        # 5. Множественное наследование (усложняет иерархию / MRO)
        if len(node.bases) > 2:
            self.add(node, "OOP", "MULTI-INH",
                     f"Класс '{node.name}': наследуется сразу от {len(node.bases)} классов — "
                     f"усложняет понимание MRO (Method Resolution Order).")

        # 6. Мутабельный class-level атрибут — расшарен между ВСЕМИ экземплярами класса
        for item in node.body:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and isinstance(item.value, (ast.List, ast.Dict, ast.Set)):
                        self.add(item, "BUG", "CLASS-MUT-ATTR",
                                 f"Класс '{node.name}': атрибут '{target.id}' — изменяемый объект "
                                 f"({type(item.value).__name__}), объявленный на уровне класса. "
                                 f"Он ОДИН на все экземпляры (классическая ловушка ООП в Python), "
                                 f"а не отдельный на каждый объект. Инициализируй его в __init__.")

        # 7. Наследник переопределяет __init__, но не вызывает super().__init__().
        # Предупреждаем, только если известно (через реестр проекта), что у родителя
        # реально есть свой __init__ - иначе для внешних библиотек будет много шума.
        if inherited_known and "__init__" in inherited_methods and has_init:
            init_method = next(m for m in methods if m.name == "__init__")
            calls_super = any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "__init__"
                and isinstance(n.func.value, ast.Call)
                and isinstance(n.func.value.func, ast.Name)
                and n.func.value.func.id == "super"
                for n in ast.walk(init_method)
            )
            if not calls_super:
                self.add(init_method, "OOP", "NO-SUPER-INIT",
                         f"Класс '{node.name}' наследуется от класса с собственным __init__, но "
                         f"свой '__init__' не вызывает super().__init__(...) — родительская часть "
                         f"объекта может остаться не инициализированной.")

        # 8. __eq__ определён без __hash__ (объект станет unhashable / сломает set/dict)
        if "__eq__" in method_names and "__hash__" not in method_names:
            self.add(node, "OOP", "EQ-NO-HASH",
                     f"Класс '{node.name}': определён '__eq__', но не '__hash__' — Python сделает "
                     f"объект unhashable (сломается использование в set/dict, если раньше работало).")

    @staticmethod
    def _decorator_name(dec):
        if isinstance(dec, ast.Name):
            return dec.id
        if isinstance(dec, ast.Attribute):
            return dec.attr
        return ""

    # ---------- общие проверки по всему дереву ----------
    def check_generic_bugs(self, tree: ast.Module):
        for node in ast.walk(tree):
            self._check_bare_except(node)
            self._check_none_compare(node)
            self._check_float_eq(node)
            self._check_shadow_builtin(node)
            self._check_while_true(node)
            self._check_identity_misuse(node)

    def _check_bare_except(self, node):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            self.add(node, "BUG", "BARE-EXCEPT",
                     "Голый 'except:' перехватывает вообще всё (включая KeyboardInterrupt) "
                     "и маскирует реальные баги. Указывай конкретный тип исключения.")
        elif isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            self.add(node, "BUG", "EXCEPT-PASS",
                     "'except ...: pass' молча проглатывает ошибку без логирования — "
                     "баг может происходить постоянно, а ты никогда не узнаешь об этом.")

    def _check_identity_misuse(self, node):
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Is, ast.IsNot)):
                    for side in (node.left, comparator):
                        if isinstance(side, ast.Constant) and not (side.value is None or isinstance(side.value, bool)):
                            self.add(node, "BUG", "IS-LITERAL",
                                     "'is' / 'is not' используется для сравнения с литералом "
                                     "(число/строка) — из-за интернирования в CPython результат "
                                     "непредсказуем. Используй '==' / '!='.")
                            break

    def _check_none_compare(self, node):
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.NotEq)) and (self._is_none(comparator) or self._is_none(node.left)):
                    self.add(node, "STYLE", "NONE-EQ",
                             "Сравнение с None лучше делать через 'is' / 'is not', а не '==' / '!='.")

    @staticmethod
    def _is_none(n):
        return isinstance(n, ast.Constant) and n.value is None

    def _check_float_eq(self, node):
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.NotEq)) and (self._is_float(comparator) or self._is_float(node.left)):
                    self.add(node, "BUG", "FLOAT-EQ",
                             "Сравнение float через '==' ненадёжно из-за погрешностей округления "
                             "(используй abs(a - b) < eps).")

    @staticmethod
    def _is_float(n):
        return isinstance(n, ast.Constant) and isinstance(n.value, float)

    def _check_shadow_builtin(self, node):
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            target = node.name
        if target and target in BUILTIN_NAMES:
            self.add(node, "STYLE", "SHADOW-BUILTIN",
                     f"Имя '{target}' совпадает со встроенным именем Python и переопределяет его в этой области видимости.")

    def _check_while_true(self, node):
        if isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value is True:
            has_break = any(isinstance(n, ast.Break) for n in ast.walk(node))
            if not has_break:
                self.add(node, "BUG", "INF-LOOP",
                         "'while True' без 'break' внутри тела — похоже на потенциально бесконечный цикл.")

    def check_unused_imports(self):
        for name, lineno in self.imported_names.items():
            if name not in self.used_names:
                self.issues.append(Issue(self.filename, lineno, "STYLE", "UNUSED-IMPORT",
                                          f"Импорт '{name}' нигде в файле не используется."))


def collect_files(paths: List[str]) -> List[str]:
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, filenames in os.walk(p):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "venv", ".venv")]
                for fn in filenames:
                    if fn.endswith(".py"):
                        files.append(os.path.join(root, fn))
        elif p.endswith(".py") and os.path.isfile(p):
            files.append(p)
        else:
            print(f"Пропускаю (не .py и не директория): {p}", file=sys.stderr)
    return sorted(set(files))


def check_file(path: str, registry: dict, max_complexity: int) -> List[Issue]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return [Issue(path, 0, "BUG", "READ-ERROR", f"Не удалось прочитать файл: {e}")]

    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as e:
        return [Issue(path, e.lineno or 0, "BUG", "SYNTAX", f"Синтаксическая ошибка: {e.msg}")]

    checker = FileChecker(path, source, registry=registry, max_complexity=max_complexity)
    checker.visit(tree)
    checker.check_generic_bugs(tree)
    checker.check_unused_imports()
    return checker.issues


def build_project_registry(files: List[str]) -> dict:
    """Первый проход по всем файлам проекта - только сбор информации о классах."""
    all_infos = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source, filename=path)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue  # проблемные файлы всё равно попадут в основной отчёт как BUG
        all_infos.extend(collect_class_infos(tree, path))
    return build_registry(all_infos)


def write_json_report(all_issues: List[Issue], json_path: str, files_checked: List[str]):
    payload = {
        "files_checked": files_checked,
        "total_issues": len(all_issues),
        "issues": [issue.as_dict() for issue in all_issues],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def write_report(all_issues: List[Issue], output_path: str, files_checked: List[str]):
    severity_order = {"BUG": 0, "OOP": 1, "STYLE": 2}
    all_issues.sort(key=lambda i: (i.file, severity_order.get(i.severity, 9), i.line))

    by_file = {}
    for issue in all_issues:
        by_file.setdefault(issue.file, []).append(issue)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("=" * 78 + "\n")
        f.write("ОТЧЁТ СТАТИЧЕСКОГО АНАЛИЗА (ООП + потенциальные баги)\n")
        f.write("=" * 78 + "\n")
        f.write(f"Проверено файлов:   {len(files_checked)}\n")
        f.write(f"Найдено замечаний:  {len(all_issues)}\n\n")

        for path in files_checked:
            f.write("-" * 78 + "\n")
            f.write(f"Файл: {path}\n")
            f.write("-" * 78 + "\n")
            file_issues = by_file.get(path, [])
            if not file_issues:
                f.write("  Замечаний не найдено.\n\n")
                continue
            for issue in file_issues:
                f.write("  " + issue.format() + "\n")
            f.write("\n")

        counts = {}
        for issue in all_issues:
            counts[issue.severity] = counts.get(issue.severity, 0) + 1
        f.write("=" * 78 + "\n")
        f.write("Итого по категориям: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) + "\n")
        f.write("(BUG = вероятный логический баг, OOP = нарушение принципов ООП, STYLE = стиль)\n")


def main():
    parser = argparse.ArgumentParser(
        description="Проверка Python-кода на принципы ООП и потенциальные логические баги."
    )
    parser.add_argument("paths", nargs="+", help="Файлы .py и/или директории проекта для проверки.")
    parser.add_argument("--output", "-o", default="checks.log", help="Путь к файлу отчёта (по умолчанию checks.log).")
    parser.add_argument("--json", action="store_true",
                         help="Дополнительно сохранить отчёт в JSON рядом с --output (то же имя, расширение .json).")
    parser.add_argument("--max-complexity", type=int, default=10,
                         help="Порог цикломатической сложности функции, после которого выводится предупреждение (по умолчанию 10).")
    args = parser.parse_args()

    files = collect_files(args.paths)
    if not files:
        print("Не найдено ни одного .py файла по указанным путям.")
        sys.exit(1)

    # Проход 1: строим реестр классов по всему проекту (для межфайлового анализа наследования)
    registry = build_project_registry(files)

    # Проход 2: собственно проверки, уже с учётом реестра
    all_issues: List[Issue] = []
    for path in files:
        all_issues.extend(check_file(path, registry, args.max_complexity))

    write_report(all_issues, args.output, files)

    if args.json:
        json_path = os.path.splitext(args.output)[0] + ".json"
        write_json_report(all_issues, json_path, files)
        print(f"JSON-отчёт сохранён в: {json_path}")

    print(f"Проверено файлов:  {len(files)}")
    print(f"Найдено замечаний: {len(all_issues)}")
    print(f"Отчёт сохранён в:  {args.output}")


if __name__ == "__main__":
    main()