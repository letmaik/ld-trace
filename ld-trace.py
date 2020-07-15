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
parser.add_argument('--direct-only', action='store_true', help='filter out traces with indirect references')
parser.add_argument('--first-only', action='store_true', help='show only the first trace for each traced symbol')
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
    if args.verbose:
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
            
        if args.verbose:
            print(' ✔️')
    else:
        if args.verbose:
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
        elif 'R_X86_64_PLT32' in line or 'R_X86_64_PC32' in line:
            assert obj
            assert section
            if sym_name is None:
                if args.trace:
                    print('-> ignoring (sym name missing)')
                continue
            parts = line.split()
            ref = parts[2]
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
            # .text. is added for local (static) symbols
            ref_sym = ref.replace('.text.', '')
            # .L., .rodata., ..-
            starts_with_dot = ref_sym.startswith('.')
            if 'R_X86_64_PC32' in line and starts_with_dot:
                if args.trace:
                    print('-> ignoring (starts with dot and is R_X86_64_PC32)')
                continue
            assert not starts_with_dot
            try:
                suffix_idx = ref_sym.index('-0x')
            except ValueError:
                if args.trace:
                    print('-> ignoring (cannot parse referenced symbol name)')
                continue
            ref_sym = ref_sym[:suffix_idx]
            refs[ref_sym].append(SymbolReference(
                referencing_sym=sym,
                referenced_sym=ref_sym,
                src=src))
            if args.trace:
                print(f'found reference: {sym_name} -> {ref_sym}')
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
    # The section/object being pulled in during linking.
    # Section if --gc-sections, otherwise Object.
    group: Union[Object, Section] 

    # All symbols in `group` that directly reference the previous symbol in the path.
    refs: FrozenSet[SymbolReference]
    
    # The symbol in `group` that directly or indirectly references the previous symbol in the path.
    # A direct reference (SymbolReference, one of `refs`) is typically a function call to the previous symbol.
    # An indirect reference (DefinedSymbol) is due to being in the same section/object as a direct reference.
    # Indirect references typically occur when -ffunction-sections/--gc-sections is not used and
    # multiple functions are part of the same source file. Calling/referencing a single function will
    # pull in all other symbols of the same source file / object.
    ref: Union[SymbolReference, DefinedSymbol]

LinkReferencePath = Tuple[LinkReference,...]

if args.require_defined:
    require_defined: Set[SymbolName] = set(args.require_defined)

def prune_link_ref_path(path: LinkReferencePath) -> LinkReferencePath:
    if len(path) == 0:
        return path
    elif args.whole_archive:
        if args.direct_only and any(isinstance(r.ref, DefinedSymbol) for r in path):
            return tuple()
        return path
    elif args.require_defined:
        found_required_at = -1
        for i, link_ref in enumerate(path[::-1]):
            if isinstance(link_ref.ref, DefinedSymbol):
                if args.direct_only:
                    return tuple()
                sym_name = link_ref.ref.name
            else:
                sym_name = link_ref.ref.referencing_sym.name
            if found_required_at == -1:
                if sym_name in require_defined:
                    found_required_at = len(path) - 1 - i
                    if not args.direct_only:
                        break
        if found_required_at == -1:
            return tuple()
        else:
            return tuple(path[:found_required_at + 1])
    
    assert False

def walk_link_ref_paths(sym_name: str, path_fn: Callable[[LinkReferencePath], bool],
                        head_path: LinkReferencePath=()) -> bool:
    if args.trace:
        print(f'ref: {sym_name}')
    sym_refs = refs[sym_name]
    if not sym_refs:
        pruned_path = prune_link_ref_path(head_path)
        if pruned_path:
            if args.verbose:
                print('path found!')
                if len(head_path) != len(pruned_path):
                    print(f'original: {head_path}')
                    print(f'pruned:   {pruned_path}')
            return path_fn(pruned_path)
        return True
    if args.gc_sections:
        def by_section_key(sym_ref: SymbolReference):
            return sym_ref.referencing_sym.section
        sym_refs = sorted(sym_refs, key=by_section_key) # groupby requires sorted iterable
        sym_refs_by_section = groupby(sym_refs, key=by_section_key)
        for section, sym_refs_ in sym_refs_by_section:
            if args.trace:
                print(f' {section.name} {section.obj.name}')
            if section in set(link_ref.group for link_ref in head_path):
                # TODO stopping at cycles leads to truncated paths which may be confusing
                if args.trace:
                    print('cycle detected, stopping here')
                    link_path = ' -> '.join(link_ref.group.name for link_ref in head_path)
                    print(link_path)
                pruned_path = prune_link_ref_path(head_path)
                if pruned_path:
                    if args.verbose:
                        print('path found! (stopped at cycle start)')
                        if len(head_path) != len(pruned_path):
                            print(f'original: {head_path}')
                            print(f'pruned:   {pruned_path}')
                    return path_fn(pruned_path)
                return True
            sym_refs_ = frozenset(sym_refs_)
            for sym in defs_grouped[section.obj][section].values():
                if args.trace:
                    print(f'  {sym.name}')
                ref = next((r for r in sym_refs_ if r.referencing_sym == sym), sym)
                link_ref = LinkReference(section, sym_refs_, ref)
                if not walk_link_ref_paths(sym.name, path_fn, (*head_path, link_ref)):
                    if args.verbose:
                        print('stopping search')
                    return False
    else:
        def by_obj_key(sym_ref: SymbolReference):
            return sym_ref.referencing_sym.section.obj
        sym_refs = sorted(sym_refs, key=by_obj_key)
        sym_refs_by_obj = groupby(sym_refs, key=by_obj_key)
        for obj, sym_refs_ in sym_refs_by_obj:
            if args.trace:
                print(f'obj: {obj.name}')
            if obj in set(link_ref.group for link_ref in head_path):
                # TODO stopping at cycles leads to truncated paths which may be confusing
                if args.trace:
                    print('cycle detected')
                    if args.trace:
                        link_path = ' -> '.join(link_ref.group.name for link_ref in head_path)
                        print(link_path)
                pruned_path = prune_link_ref_path(head_path)
                if pruned_path:
                    if args.verbose:
                        print('path found! (stopped at cycle start)')
                        if len(head_path) != len(pruned_path):
                            print(f'original: {head_path}')
                            print(f'pruned:   {pruned_path}')
                    return path_fn(pruned_path)
                return True
            sym_refs_ = frozenset(sym_refs_)
            for section, section_syms in defs_grouped[obj].items():
                if args.trace:
                    print(f'section: {section.name}')
                for sym in section_syms.values():
                    if args.trace:
                        print(f'{obj.name} {section.name} {sym.name}')
                    ref = next((r for r in sym_refs_ if r.referencing_sym == sym), sym)
                    link_ref = LinkReference(obj, sym_refs_, ref)
                    if not walk_link_ref_paths(sym.name, path_fn, (*head_path, link_ref)):
                        if args.verbose:
                            print('stopping search')
                        return False
    return True

def print_link_ref_path(path: LinkReferencePath):
    for i, link_ref in enumerate(path):
        if isinstance(link_ref.group, Object):
            print(f'#{len(path)-i-1}  {link_ref.group.name} ({fmt_path(link_ref.group.archive)})')
        else: # Section
            print(f'#{len(path)-i-1}  {link_ref.group.name} ({link_ref.group.obj.name} {fmt_path(link_ref.group.obj.archive)})')
        
        def get_prefix(sym: DefinedSymbol):
            if sym.is_global and args.require_defined and sym.name in require_defined:
                return '!'
            else:
                return ' '

        if isinstance(link_ref.ref, SymbolReference):
            # Direct reference, print only that.
            sym = link_ref.ref.referencing_sym
            print(f'{get_prefix(sym)}^*   {sym.name} {fmt_path(link_ref.ref.src)}')
        else:
            # Indirect reference, print that and all direct references for context.
            sym = link_ref.ref
            print(f'{get_prefix(sym)}.*   {sym.name} {fmt_path(link_ref.ref.src)}')
            for sym_ref in sorted(link_ref.refs, key=lambda sym_ref: sym_ref.referencing_sym.name + sym_ref.src):
                sym = sym_ref.referencing_sym
                print(f'{get_prefix(sym)}^    {sym.name} {fmt_path(sym_ref.src)}')

print()
for sym_name in args.trace_symbol:
    syms = global_defs[sym_name] or defs[sym_name]
    if len(syms) == 0:
        loc = '(undefined)'
        src = ''
    elif len(syms) == 1:
        if args.gc_sections:
            loc = f'{syms[0].section.name} ({syms[0].section.obj.name} {fmt_path(syms[0].section.obj.archive)})'
        else:
            loc = f'{syms[0].section.obj.name} ({fmt_path(syms[0].section.obj.archive)})'
        src = syms[0].src
    else:
        loc = '(multiple defs)'
        src = ''
    seen = set()
    def on_path_found(path: LinkReferencePath):
        # Pruning can produce duplicate paths, only print once.
        if path in seen:
            return True
        seen.add(path)
        global found
        found = True
        print(f'#{len(path)}  {loc}')
        print(f'  *   {sym_name} {fmt_path(src)}')
        print_link_ref_path(path)
        print()
        if args.first_only:
            return False # stop
        return True # keep going
    walk_link_ref_paths(sym_name, on_path_found)
    if not seen:
        print(f'No paths found for {sym_name}.')
        print()