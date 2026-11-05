#!/usr/bin/env python3
"""
oopCheck.py

Статичний аналізатор Python-коду:
 - Перевіряє базові принципи ООП (інкапсуляція, розмір класу, використання self,
   наслідування, відсутні/"неповні" атрибути тощо)
 - Шукає потенційні логічні баги, які звичайний запуск у терміналі
   не покаже (mutable default args, bare except, порівняння з None через ==,
   порівняння float через ==, невикористані імпорти, потенційно
   нескінченні цикли, неузгоджені return тощо)
 - Підтримує перевірку одразу кількох файлів
 - Підтримує перевірку цілого проєкту (директорії зі структурою пакетів)
 - Записує результат у checks.log

Використання:
    python3 oopCheck.py file1.py file2.py
    python3 oopCheck.py ./my_project/
    python3 oopCheck.py ./my_project/ file1.py --output my_report.log
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

# Межі "власної" області видимості: під час обходу тіла функції не заходимо
# всередину вкладених функцій/класів - для них буде окремий обхід.
_SCOPE_BOUNDARY = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def iter_own_scope(node):
    """Обходить піддерево вузла, не спускаючись у тіла вкладених функцій/класів."""
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
    """Інформація про один клас для міжфайлового аналізу наслідування."""
    name: str
    file: str
    lineno: int
    bases: list       # імена базових класів (рядки), як їх записано в коді
    methods: set
    assigned_attrs: set
    has_init: bool


def _base_names(node: ast.ClassDef):
    names = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            names.append(b.id)
        elif isinstance(b, ast.Attribute):
            names.append(b.attr)  # module.Class -> беремо лише "Class"
    return names


def collect_class_infos(tree: ast.Module, filename: str):
    """Прохід 1: знаходить усі класи файлу та їхню сиру інформацію (без наслідування)."""
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
        registry.setdefault(info.name, info)  # при колізії перемагає перше ім'я
    return registry


def resolve_ancestors(info: ClassInfo, registry: dict, visited=None):
    """
    Прохід 2: рекурсивно піднімається ланцюжком наслідування через реєстр.
    Повертає (fully_known, merged_attrs, merged_methods):
      fully_known=False, якщо хоча б одного предка не знайдено в проєкті
      (зовнішня бібліотека) - у такому разі "об'єднаним" даним довіряти не можна.
    """
    visited = visited or set()
    if info.name in visited:
        return True, set(), set()  # захист від циклів в оголошенні (A(B) + B(A))
    visited.add(info.name)

    merged_attrs = set(info.assigned_attrs)
    merged_methods = set(info.methods)
    fully_known = True

    for base_name in info.bases:
        if base_name == "object":
            continue
        base_info = registry.get(base_name)
        if base_info is None:
            fully_known = False  # батьківський клас поза проєктом - далі не довіряємо
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
        self.registry = registry or {}       # реєстр класів усього проєкту (для міжфайлового аналізу)
        self.max_complexity = max_complexity

    def add(self, node, severity, code, message):
        self.issues.append(Issue(self.filename, getattr(node, "lineno", 0), severity, code, message))

    # ---------- відстеження використання імен (для unused-import) ----------
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

    # ---------- функції ----------
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
                         f"Функція '{node.name}': змінюване значення за замовчуванням "
                         f"({type(default).__name__}) — спільний стан між викликами функції.")

    def _check_return_consistency(self, node):
        returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
        has_value = any(r.value is not None for r in returns)
        has_empty = any(r.value is None for r in returns)
        if has_value and has_empty:
            self.add(node, "BUG", "RET-MIX",
                     f"Функція '{node.name}': в одних return є значення, в інших — немає. "
                     f"Можлива неявна логічна помилка (випадковий None).")

    def _check_too_many_args(self, node):
        args = node.args.args
        count = len(args)
        if args and args[0].arg in ("self", "cls"):
            count -= 1
        if count > 6:
            self.add(node, "OOP", "TOO-MANY-ARGS",
                     f"Функція/метод '{node.name}': {count} параметрів — забагато, "
                     f"складно викликати й тестувати. Розглянь групування в об'єкт/dataclass.")

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
                                          f"У '{node.name}': змінній '{name}' присвоюється значення, але "
                                          f"далі вона ніде не читається — імовірно, забута логіка або друкарська помилка в імені."))

    def _check_complexity(self, node):
        """
        Цикломатична складність (McCabe): 1 (базовий шлях через функцію) +
        1 за кожну точку розгалуження (if/elif, for, while, except, and/or,
        тернарний оператор, умова в comprehension, case у match).
        Вкладені функції/класи рахуються окремо, до загального рахунку не входять.
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
                pass  # самі case нижче
            elif n.__class__.__name__ == "match_case":
                # виключаємо "case _:" (wildcard, не вважається гілкою-рішенням)
                pattern = getattr(n, "pattern", None)
                if not (pattern is not None and pattern.__class__.__name__ == "MatchAs" and pattern.pattern is None):
                    complexity += 1

        if complexity > self.max_complexity:
            self.add(node, "BUG", "HIGH-COMPLEXITY",
                     f"Функція '{node.name}': цикломатична складність {complexity} "
                     f"(поріг {self.max_complexity}) — забагато незалежних шляхів виконання, "
                     f"складно покрити тестами й легко пропустити баг у рідкісній гілці.")

    # ---------- класи / ООП ----------
    def visit_ClassDef(self, node):
        self._check_class(node)
        self.generic_visit(node)

    def _check_class(self, node: ast.ClassDef):
        methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        method_names = {m.name for m in methods}
        has_init = "__init__" in method_names

        # 1. Іменування класу (PascalCase)
        if not node.name[:1].isupper() or "_" in node.name:
            self.add(node, "OOP", "NAMING",
                     f"Клас '{node.name}': ім'я класу прийнято писати в PascalCase (CapWords).")

        # 2. Евристика God-class (порушення Single Responsibility)
        if len(methods) > 15:
            self.add(node, "OOP", "GOD-CLASS",
                     f"Клас '{node.name}': {len(methods)} методів — імовірно, клас робить "
                     f"забагато (порушення Single Responsibility Principle).")

        # 3. self.attr: читається vs присвоюється (інкапсуляція / потенційний AttributeError).
        # Враховуємо атрибути, успадковані від батьків у всьому проєкті (див. resolve_ancestors).
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

        if inherited_known:  # якщо хоч один батьківський клас "не видно" - не ризикуємо хибним спрацюванням
            missing = read_attrs - assigned_attrs - method_names - inherited_attrs - inherited_methods
            for attr in sorted(missing):
                self.add(node, "BUG", "ATTR-MISS",
                         f"Клас '{node.name}': 'self.{attr}' читається, але ніде в класі (і його "
                         f"відомих батьківських класах у проєкті) явно не присвоюється — можливий "
                         f"AttributeError під час виконання.")

        if not has_init and assigned_attrs:
            self.add(node, "OOP", "NO-INIT",
                     f"Клас '{node.name}': атрибути створюються поза __init__ — об'єкт може "
                     f"опинитися в неповному/непередбачуваному стані залежно від порядку викликів.")

        # 4. Метод не використовує self -> кандидат у @staticmethod
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
                         f"Метод '{node.name}.{m.name}': не використовує 'self' — імовірно, "
                         f"варто зробити його @staticmethod.")

        # 5. Множинне наслідування (ускладнює ієрархію / MRO)
        if len(node.bases) > 2:
            self.add(node, "OOP", "MULTI-INH",
                     f"Клас '{node.name}': успадковується одразу від {len(node.bases)} класів — "
                     f"ускладнює розуміння MRO (Method Resolution Order).")

        # 6. Змінюваний атрибут рівня класу — спільний для ВСІХ екземплярів класу
        for item in node.body:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and isinstance(item.value, (ast.List, ast.Dict, ast.Set)):
                        self.add(item, "BUG", "CLASS-MUT-ATTR",
                                 f"Клас '{node.name}': атрибут '{target.id}' — змінюваний об'єкт "
                                 f"({type(item.value).__name__}), оголошений на рівні класу. "
                                 f"Він ОДИН на всі екземпляри (класична пастка ООП у Python), "
                                 f"а не окремий для кожного об'єкта. Ініціалізуй його в __init__.")

        # 7. Нащадок перевизначає __init__, але не викликає super().__init__().
        # Попереджаємо, лише якщо відомо (через реєстр проєкту), що в батьківського класу
        # справді є власний __init__ - інакше для зовнішніх бібліотек буде багато шуму.
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
                         f"Клас '{node.name}' успадковується від класу з власним __init__, але "
                         f"свій '__init__' не викликає super().__init__(...) — батьківська частина "
                         f"об'єкта може залишитися неініціалізованою.")

        # 8. __eq__ визначено без __hash__ (об'єкт стане unhashable / зламає set/dict)
        if "__eq__" in method_names and "__hash__" not in method_names:
            self.add(node, "OOP", "EQ-NO-HASH",
                     f"Клас '{node.name}': визначено '__eq__', але не '__hash__' — Python зробить "
                     f"об'єкт unhashable (зламається використання в set/dict, якщо раніше працювало).")

    @staticmethod
    def _decorator_name(dec):
        if isinstance(dec, ast.Name):
            return dec.id
        if isinstance(dec, ast.Attribute):
            return dec.attr
        return ""

    # ---------- загальні перевірки по всьому дереву ----------
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
                     "Голий 'except:' перехоплює взагалі все (зокрема KeyboardInterrupt) "
                     "і маскує справжні баги. Вказуй конкретний тип винятку.")
        elif isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            self.add(node, "BUG", "EXCEPT-PASS",
                     "'except ...: pass' мовчки ковтає помилку без логування — "
                     "баг може відбуватися постійно, а ти ніколи про це не дізнаєшся.")

    def _check_identity_misuse(self, node):
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Is, ast.IsNot)):
                    for side in (node.left, comparator):
                        if isinstance(side, ast.Constant) and not (side.value is None or isinstance(side.value, bool)):
                            self.add(node, "BUG", "IS-LITERAL",
                                     "'is' / 'is not' використовується для порівняння з літералом "
                                     "(число/рядок) — через інтернування в CPython результат "
                                     "непередбачуваний. Використовуй '==' / '!='.")
                            break

    def _check_none_compare(self, node):
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.NotEq)) and (self._is_none(comparator) or self._is_none(node.left)):
                    self.add(node, "STYLE", "NONE-EQ",
                             "Порівняння з None краще робити через 'is' / 'is not', а не '==' / '!='.")

    @staticmethod
    def _is_none(n):
        return isinstance(n, ast.Constant) and n.value is None

    def _check_float_eq(self, node):
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.NotEq)) and (self._is_float(comparator) or self._is_float(node.left)):
                    self.add(node, "BUG", "FLOAT-EQ",
                             "Порівняння float через '==' ненадійне через похибки округлення "
                             "(використовуй abs(a - b) < eps).")

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
                     f"Ім'я '{target}' збігається з вбудованим ім'ям Python і перевизначає його в цій області видимості.")

    def _check_while_true(self, node):
        if isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value is True:
            has_break = any(isinstance(n, ast.Break) for n in ast.walk(node))
            if not has_break:
                self.add(node, "BUG", "INF-LOOP",
                         "'while True' без 'break' усередині тіла — схоже на потенційно нескінченний цикл.")

    def check_unused_imports(self):
        for name, lineno in self.imported_names.items():
            if name not in self.used_names:
                self.issues.append(Issue(self.filename, lineno, "STYLE", "UNUSED-IMPORT",
                                          f"Імпорт '{name}' ніде у файлі не використовується."))


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
            print(f"Пропускаю (не .py і не директорія): {p}", file=sys.stderr)
    return sorted(set(files))


def check_file(path: str, registry: dict, max_complexity: int) -> List[Issue]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return [Issue(path, 0, "BUG", "READ-ERROR", f"Не вдалося прочитати файл: {e}")]

    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as e:
        return [Issue(path, e.lineno or 0, "BUG", "SYNTAX", f"Синтаксична помилка: {e.msg}")]

    checker = FileChecker(path, source, registry=registry, max_complexity=max_complexity)
    checker.visit(tree)
    checker.check_generic_bugs(tree)
    checker.check_unused_imports()
    return checker.issues


def build_project_registry(files: List[str]) -> dict:
    """Перший прохід по всіх файлах проєкту - лише збір інформації про класи."""
    all_infos = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source, filename=path)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue  # проблемні файли все одно потраплять в основний звіт як BUG
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
        f.write("ЗВІТ СТАТИЧНОГО АНАЛІЗУ (ООП + потенційні баги)\n")
        f.write("=" * 78 + "\n")
        f.write(f"Перевірено файлів:   {len(files_checked)}\n")
        f.write(f"Знайдено зауважень:  {len(all_issues)}\n\n")

        for path in files_checked:
            f.write("-" * 78 + "\n")
            f.write(f"Файл: {path}\n")
            f.write("-" * 78 + "\n")
            file_issues = by_file.get(path, [])
            if not file_issues:
                f.write("  Зауважень не знайдено.\n\n")
                continue
            for issue in file_issues:
                f.write("  " + issue.format() + "\n")
            f.write("\n")

        counts = {}
        for issue in all_issues:
            counts[issue.severity] = counts.get(issue.severity, 0) + 1
        f.write("=" * 78 + "\n")
        f.write("Разом за категоріями: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) + "\n")
        f.write("(BUG = імовірний логічний баг, OOP = порушення принципів ООП, STYLE = стиль)\n")


def main():
    parser = argparse.ArgumentParser(
        description="Перевірка Python-коду на принципи ООП і потенційні логічні баги."
    )
    parser.add_argument("paths", nargs="+", help="Файли .py та/або директорії проєкту для перевірки.")
    parser.add_argument("--output", "-o", default="checks.log", help="Шлях до файлу звіту (за замовчуванням checks.log).")
    parser.add_argument("--json", action="store_true",
                         help="Додатково зберегти звіт у JSON поруч із --output (те саме ім'я, розширення .json).")
    parser.add_argument("--max-complexity", type=int, default=10,
                         help="Поріг цикломатичної складності функції, після якого виводиться попередження (за замовчуванням 10).")
    args = parser.parse_args()

    files = collect_files(args.paths)
    if not files:
        print("Не знайдено жодного .py файлу за вказаними шляхами.")
        sys.exit(1)

    # Прохід 1: будуємо реєстр класів по всьому проєкту (для міжфайлового аналізу наслідування)
    registry = build_project_registry(files)

    # Прохід 2: власне перевірки, уже з урахуванням реєстру
    all_issues: List[Issue] = []
    for path in files:
        all_issues.extend(check_file(path, registry, args.max_complexity))

    write_report(all_issues, args.output, files)

    if args.json:
        json_path = os.path.splitext(args.output)[0] + ".json"
        write_json_report(all_issues, json_path, files)
        print(f"JSON-звіт збережено в: {json_path}")

    print(f"Перевірено файлів:  {len(files)}")
    print(f"Знайдено зауважень: {len(all_issues)}")
    print(f"Звіт збережено в:   {args.output}")


if __name__ == "__main__":
    main()
