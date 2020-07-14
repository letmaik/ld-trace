# A script to determine how a symbol ends up in a link.
# Like -y/--trace-symbol, but better.

# Examples:
# python ld-trace.py -y func --whole-archive -L. -lfoo
# python ld-trace.py -y func --entry=main mymain.o libfoo.a
# python ld-trace.py -y func --whole-archive --gc-sections mymain.o libfoo.a

# Limitations:
# - Linux only, relies on objdump and nm.
# - Tested on object (.o) and archive (.a) files only.
# - Object files are currently treated the same as archive files.
#   Without --whole-archive, object files should still always be included.
# - Unsupported flags are ignored and reported, for example:
#   --exclude-libs lib,lib,...: only '--exclude-libs ALL' is supported.
#   -T/--script: Linker scripts are not supported.
#   --start-group/--end-group: Symbols are always globally resolved, like in lld.

from typing import FrozenSet, Union, NamedTuple, DefaultDict, List, Dict, Set, AbstractSet, Tuple, Callable
from collections import defaultdict
import argparse
import subprocess
import os
import sys
from hashlib import sha256
from itertools import groupby

# Archive
#  Object
#   Section
#    Symbol

class Object(NamedTuple):
    name: str
    archive: str

class Section(NamedTuple):
    name: str
    obj: Object

class DefinedSymbol(NamedTuple):
    name: str
    type: str
    is_global: bool
    src: str
    section: Section

class SymbolReference(NamedTuple):
    referencing_sym: DefinedSymbol
    referenced_sym: str
    src: str

# TODO treat .o files differently than .a files

parser = argparse.ArgumentParser()

# ld flags
parser.add_argument('files', metavar='file', nargs='+', help='.o/.a files, order does not matter')
parser.add_argument('-l', '--library', dest='libraries', metavar='namespec',
                    action='append', default=[], help='.a files, order does not matter')
parser.add_argument('-L', '--library-path', dest='search_dirs', metavar='searchdir',
                    action='append', default=[])
parser.add_argument('-y', '--trace-symbol', metavar='SYMBOL', action='append', required=True)
parser.add_argument('-shared', action='store_true')
parser.add_argument('--whole-archive', action='store_true', help='applies to all archives')
parser.add_argument('--exclude-libs')
parser.add_argument('--gc-sections', action='store_true')
parser.add_argument('--require-defined', metavar='SYMBOL', action='append')
parser.add_argument('-e', '--entry', metavar='SYMBOL', help='behaves the same as --require-defined')
parser.add_argument('--fatal-warnings', action='store_true')
parser.add_argument('--verbose', action='store_true', help='diagnostic output')
parser.add_argument('-t', '--trace', action='store_true', help='more diagnostic output')

# custom flags
parser.add_argument('--strip-prefix', help='remove given path prefix in output')
parser.add_argument('--cache-dir', default=os.path.expanduser('~/.cache/ld-trace'), help='cache directory')
parser.add_argument('--refresh-cache', action='store_true')
parser.add_argument('--no-cache', action='store_true')

args, remaining = parser.parse_known_args()

def exit_if_fatal_warnings():
    if args.fatal_warnings:
        print('Stopping as --fatal-warnings was given')
        sys.exit(1)

if remaining:
    print('WARNING: The following flags have been ignored:')
    print(' '.join(remaining))
    exit_if_fatal_warnings()

if args.shared:
    args.whole_archive = True

if args.exclude_libs:
    if args.exclude_libs == 'ALL':
        args.whole_archive = False
    else:
        print('WARNING: ignoring --exclude-libs, only --exclude-libs ALL is supported')

if args.whole_archive:
    args.require_defined = None
else:
    if args.entry:
        args.require_defined.append(args.entry)

    if not args.require_defined:
        print('--whole-archive/--require-defined/--entry not given, defaulting to --entry _start')
        args.require_defined = ['_start']

if not args.no_cache:
    os.makedirs(args.cache_dir, exist_ok=True)

def run(args_: List[str], depends: List[str]) -> str:
    assert len(depends) > 0
    cmd = ' '.join(args_)
    print(f'> {cmd}', end='', flush=True)
    out = None
    cache_path = None
    mtime = None
    if not args.no_cache:
        mtime = max(os.path.getmtime(path) for path in depends)
        sha = sha256(cmd.encode()).hexdigest()
        cache_path = os.path.join(args.cache_dir, sha)
        if not args.refresh_cache and os.path.exists(cache_path) and os.path.getmtime(cache_path) == mtime:
            with open(cache_path) as f:
                out = f.read()
    if out is None:       
        out = subprocess.run(args_, capture_output=True, check=True, universal_newlines=True).stdout
        if not args.no_cache:
            assert cache_path
            assert mtime
            with open(cache_path, 'w') as f:
                f.write(out)
            os.utime(cache_path, (mtime, mtime))
            
        print(' ✔️')
    else:
        print(' ✔️  (cached)')        
    return out

def fmt_path(p):
    if p.endswith('.a'):
        return os.path.basename(p)
    if args.strip_prefix:
        p = p.replace(args.strip_prefix, '')
    return p

SymbolName = str

defs: DefaultDict[SymbolName, List[DefinedSymbol]] = defaultdict(list)
global_defs: DefaultDict[SymbolName, List[DefinedSymbol]] = defaultdict(list)
defs_grouped: DefaultDict[Object, DefaultDict[Section, Dict[SymbolName, DefinedSymbol]]] = \
    defaultdict(lambda: defaultdict(dict))
global_defs_grouped: DefaultDict[Object, DefaultDict[Section, Dict[SymbolName, DefinedSymbol]]] = \
    defaultdict(lambda: defaultdict(dict))

refs: DefaultDict[SymbolName, List[SymbolReference]] = defaultdict(list)

for namespec in args.libraries:
    found = False
    filename = f'lib{namespec}.a'
    for search_dir in args.search_dirs:
        path = os.path.join(search_dir, filename)
        if os.path.exists(path):
            args.files.append(path)
            found = True
            break
    if not found:
        print(f'library {filename} not found in search dirs:')
        print(args.search_dirs)
        sys.exit(1)

for archive in args.files:
    # nm does not provide section names, objdump does not provide source/line info, use both.
    nm_out = run(['nm', '--defined-only', '--line-numbers', archive], depends=[archive])
    objdump_out = run(['objdump', '--syms', archive], depends=[archive])

    # parse objdump output to create symbol -> section mapping
    sym_section_map: DefaultDict[Object, Dict[SymbolName, str]] = defaultdict(dict)
    obj = None
    sym_table_found = False
    for line in objdump_out.splitlines():
        if args.trace:
            print(f'LINE: {line}')
        # a.o:     file format elf64-x86-64
        #
        # SYMBOL TABLE:
        # 0000000000000000 l    df *ABS*  0000000000000000 a.c
        # 0000000000000000 g     F .text  0000000000000010 a1
        if line.endswith('file format elf64-x86-64'):
            obj = Object(line.split()[0][:-1], archive)
            sym_table_found = False
        elif line.startswith('SYMBOL TABLE:'):
            sym_table_found = True
        elif sym_table_found:
            assert obj
            if not line:
                sym_table_found = False
                obj = None
                continue
            first_space_idx = line.index(' ') # allow variable address size
            section_name_idx = first_space_idx + 1 + 7 + 1 # skip over flags and whitespace
            parts = line[section_name_idx:].split()
            section_name = parts[0]
            if section_name == '*UND*' or section_name == '*ABS*':
                continue
            sym_name = parts[-1]
            sym_section_map[obj][sym_name] = section_name

    # parse nm output to assemble symbol instances
    obj = None
    section = None
    for line in nm_out.splitlines():
        if not line:
            continue
        if args.trace:
            print(f'LINE: {line}')
        parts = line.split()
        if len(parts) == 1:
            # object start
            # a.o:
            obj = Object(parts[0][:-1], archive)
        else:
            # symbol
            # 0000000000000000 T a1	/.../linker-tests/a.c:1
            assert obj
            typ = parts[1]
            sym_name = parts[2]
            src = parts[3] if len(parts) == 4 else '?'
            is_global = parts[1] in ['T', 'W']
            if typ not in ['T', 'W', 't']:
                if args.verbose:
                    print(f'NOTE: ignoring {typ} definition of {sym_name} ({fmt_path(obj.archive)})')
                continue
            if typ == 't' and '.text.' in sym_name:
                if args.verbose:
                    print(f'NOTE: ignoring {typ} definition of {sym_name} ({fmt_path(obj.archive)})')
                continue
            section_name = sym_section_map[obj][sym_name]
            section = Section(section_name, obj)
            assert section
            def_symbol = DefinedSymbol(sym_name, typ, is_global, src, section)
            defs[sym_name].append(def_symbol)
            defs_grouped[obj][section][sym_name] = def_symbol
            if is_global:
                global_defs[sym_name].append(def_symbol)
                global_defs_grouped[obj][section][sym_name] = def_symbol  

for name, syms in global_defs.items():
    if sum(sym.type == 'T' for sym in syms) <= 1:
        continue
    print(f'WARNING: multiple global non-weak definitions of {name}:')
    for sym in syms:
        print(f'  {sym.type} {fmt_path(sym.src)} ({sym.section.obj.name} {fmt_path(sym.section.obj.archive)})')
    exit_if_fatal_warnings()

if args.require_defined:
    for sym_name in args.require_defined:
        if sym_name not in defs:
            raise RuntimeError(f'required symbol {sym_name} not found in global defined symbols')

obj = None
section = None
sym_name = None
src = '?'
sym_not_found_warnings_printed = set()
for archive in args.files:
    objdump_out = run(['objdump', '--reloc', '--line-numbers', archive], depends=[archive])
    for line in objdump_out.splitlines():
        if not line:
            continue
        if args.trace:
            print(f'LINE: {line}')
        # a.o:     file format elf64-x86-64
        # RELOCATION RECORDS FOR [.text.a2]:
        # OFFSET           TYPE              VALUE 
        # a2():
        # /.../linker-tests/a.c:8 (discriminator 0)
        # 000000000000000a R_X86_64_PLT32    b-0x0000000000000004
        if line.endswith('file format elf64-x86-64'):
            obj = Object(line.split()[0][:-1], archive)
            section = None
        elif line.startswith('RELOCATION RECORDS'):
            sym_name = None
            src = '?'
            section_idx = line.index('[')
            section_name = line[section_idx + 1:-2]
            assert obj
            section = Section(section_name, obj)
        elif line.endswith('():'):
            sym_name = line[:-3]
        elif 'discriminator' in line:
            src = line.split()[0]
        elif 'R_X86_64_PLT32' in line:
            assert obj
            assert section
            assert sym_name
            parts = line.split()
            ref = parts[2]
            # .text. is added for local (static) symbols
            ref_sym = ref.replace('.text.', '')
            suffix_idx = ref_sym.index('-0x')
            ref_sym = ref_sym[:suffix_idx]
            sym = None
            if sym_name in defs_grouped[obj][section]:
                sym = defs_grouped[obj][section][sym_name]
            else:              
                if sym_name not in sym_not_found_warnings_printed:
                    sym_not_found_warnings_printed.add(sym_name)
                    # This may happen with static inline definitions.
                    print(f'Note: referencing symbol {sym_name} ({section.name}) not found in definitions, ignoring')
                    if args.trace:
                        print(f'{obj.name} ({obj.archive})')
                        for section, syms in defs_grouped[obj].items():
                            print(f' {section.name}')
                            for sym_ in syms.values():
                                print(f'   {sym_.name}')
                continue
            assert sym
            refs[ref_sym].append(SymbolReference(
                referencing_sym=sym,
                referenced_sym=ref_sym,
                src=src))
        else:
            if args.trace:
                print('-> ignoring')

if args.verbose:
    print('==== symbols and direct references ====')
    for obj, sections in defs_grouped.items():
        print(f'{obj.name} ({fmt_path(obj.archive)})')
        for section, syms in sections.items():
            print(f' {section.name}')
            for sym in syms.values():
                print(f'   {sym.name}() @ {fmt_path(sym.src)}')
                for ref in refs[sym.name]:
                    print(f'    ref by {ref.referencing_sym.name}() @ {fmt_path(ref.src)}')
    print('==== end symbols and direct references ====')

class LinkReference(NamedTuple):
    group: Union[Object, Section] # Section if --gc-sections, otherwise Object
    refs: FrozenSet[SymbolReference]

LinkReferencePath = Tuple[LinkReference,...]

if args.require_defined:
    require_defined: Set[SymbolName] = set(args.require_defined)

def prune_link_ref_path(path: LinkReferencePath) -> LinkReferencePath:
    if len(path) == 0 or args.whole_archive:
        return path
    elif args.require_defined:
        found_required_at = -1
        for i, link_ref in enumerate(path[::-1]):
            path_syms: AbstractSet[SymbolName]
            if isinstance(link_ref.group, Section):
                section = link_ref.group
                path_syms = global_defs_grouped[section.obj][section].keys()
            else: # Object
                obj = link_ref.group
                path_syms = set()
                for section_syms in global_defs_grouped[obj].values():
                    path_syms |= section_syms.keys()
            if not require_defined.isdisjoint(path_syms):
                found_required_at = len(path) - 1 - i
                break
        if found_required_at == -1:
            return tuple()
        else:
            return tuple(path[:found_required_at + 1])
    assert False

def walk_link_ref_paths(sym_name: str, path_fn: Callable[[LinkReferencePath], None],
                        head_path: LinkReferencePath=()) -> None:
    if args.verbose:
        print(f'{sym_name}')
    sym_refs = refs[sym_name]
    if not sym_refs:
        pruned_path = prune_link_ref_path(head_path)
        if pruned_path:
            path_fn(pruned_path)
        return
    if args.gc_sections:
        def by_section_key(sym_ref: SymbolReference):
            return sym_ref.referencing_sym.section
        sym_refs = sorted(sym_refs, key=by_section_key) # groupby requires sorted iterable
        sym_refs_by_section = groupby(sym_refs, key=by_section_key)
        for section, sym_refs_ in sym_refs_by_section:
            if section in set(link_ref.group for link_ref in head_path):
                continue # cycle detected
            link_ref = LinkReference(section, frozenset(sym_refs_))
            if args.verbose:
                print(f' {section.name} {section.obj.name}')
            for sym_name_ in defs_grouped[section.obj][section].keys():
                if args.verbose:
                    print(f'  {sym_name_}')
                walk_link_ref_paths(sym_name_, path_fn, (*head_path, link_ref))
    else:
        def by_obj_key(sym_ref: SymbolReference):
            return sym_ref.referencing_sym.section.obj
        sym_refs = sorted(sym_refs, key=by_obj_key)
        sym_refs_by_obj = groupby(sym_refs, key=by_obj_key)
        for obj, sym_refs_ in sym_refs_by_obj:
            if obj in set(link_ref.group for link_ref in head_path):
                continue # cycle detected
            link_ref = LinkReference(obj, frozenset(sym_refs_))
            if args.verbose:
                print(f' {obj.name}')
            for section_syms in defs_grouped[obj].values():
                for sym_name_ in section_syms.keys():
                    if args.verbose:
                        print(f'  {sym_name_}')
                    walk_link_ref_paths(sym_name_, path_fn, (*head_path, link_ref))

def print_link_ref_path(path: LinkReferencePath):
    for i, link_ref in enumerate(path):
        if isinstance(link_ref.group, Object):
            print(f'#{i+1} {link_ref.group.name} ({fmt_path(link_ref.group.archive)})')
            path_syms = set()
            for section_syms in global_defs_grouped[link_ref.group].values():
                path_syms |= section_syms.keys()
        else: # Section
            print(f'#{i+1} {link_ref.group.name} ({link_ref.group.obj.name} {fmt_path(link_ref.group.obj.archive)})')
            path_syms = global_defs_grouped[link_ref.group.obj][link_ref.group].keys()
        global_ref_sym_names = set()
        for sym_ref in sorted(link_ref.refs, key=lambda sym_ref: sym_ref.referencing_sym.name + sym_ref.src):
            sym = sym_ref.referencing_sym
            if sym.is_global and args.require_defined and sym.name in require_defined:
                prefix = '*'
                global_ref_sym_names.add(sym.name)
            else:
                prefix = ' '            
            print(f'  {prefix}{sym.name} {fmt_path(sym_ref.src)}')
        if args.require_defined:
            for require_defined_sym in sorted(require_defined & path_syms - global_ref_sym_names):
                print(f'  *{require_defined_sym}')

print()
for sym_name in args.trace_symbol:
    # Paths can be duplicated if a symbol is referenced by another symbol
    # from multiple locations in the same object. Report only once.
    seen = set()
    def on_path_found(path: LinkReferencePath):
        path_groups = tuple(p.group for p in path)
        if path_groups in seen:
            return
        seen.add(path_groups)
        print(f'#0 {sym_name}')
        print_link_ref_path(path)
        print()
    walk_link_ref_paths(sym_name, on_path_found)
    if not seen:
        print(f'No paths found for {sym_name}.')
        print()