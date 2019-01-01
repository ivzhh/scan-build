# -*- coding: utf-8 -*-
#                     The LLVM Compiler Infrastructure
#
# This file is distributed under the University of Illinois Open Source
# License. See LICENSE.TXT for details.
""" This module is responsible to generate 'index.html' for the report.

The input for this step is the output directory, where individual reports
could be found. It parses those reports and generates 'index.html'. """

import re
import os
import os.path
import sys
import shutil
import plistlib
import glob
import json
import logging
import datetime
import getpass
import socket
import argparse  # noqa: ignore=F401
from typing import Dict, List, Tuple, Callable, Any, Set, Generator, Iterator, Optional  # noqa: ignore=F401
from libscanbuild.clang import get_version

__all__ = ['document']


def document(args):
    # type: (argparse.Namespace) -> int
    """ Generates cover report and returns the number of bugs/crashes. """

    html_reports_available = args.output_format in {'html', 'plist-html'}

    logging.debug('count crashes and bugs')
    crash_count = sum(1 for _ in Crash.read(args.output))
    bug_counter = create_counters()
    for bug in read_bugs(args.output, html_reports_available):
        bug_counter(bug)
    result = crash_count + bug_counter.total

    if html_reports_available and result:
        use_cdb = os.path.exists(args.cdb)

        logging.debug('generate index.html file')
        # common prefix for source files to have sorter path
        prefix = commonprefix_from(args.cdb) if use_cdb else os.getcwd()
        # assemble the cover from multiple fragments
        fragments = []
        try:
            if bug_counter.total:
                fragments.append(bug_summary(args.output, bug_counter))
                fragments.append(bug_report(args.output, prefix))
            if crash_count:
                fragments.append(crash_report(args.output, prefix))
            assemble_cover(args, prefix, fragments)
            # copy additional files to the report
            copy_resource_files(args.output)
            if use_cdb:
                shutil.copy(args.cdb, args.output)
        finally:
            for fragment in fragments:
                os.remove(fragment)
    return result


def assemble_cover(args, prefix, fragments):
    # type: (argparse.Namespace, str, List[str]) -> None
    """ Put together the fragments into a final report. """

    if args.html_title is None:
        args.html_title = os.path.basename(prefix) + ' - analyzer results'

    with open(os.path.join(args.output, 'index.html'), 'w') as handle:
        indent = 0
        handle.write(reindent("""
        |<!DOCTYPE html>
        |<html>
        |  <head>
        |    <title>{html_title}</title>
        |    <link type="text/css" rel="stylesheet" href="scanview.css"/>
        |    <script type='text/javascript' src="sorttable.js"></script>
        |    <script type='text/javascript' src='selectable.js'></script>
        |  </head>""", indent).format(html_title=args.html_title))
        handle.write(comment('SUMMARYENDHEAD'))
        handle.write(reindent("""
        |  <body>
        |    <h1>{html_title}</h1>
        |    <table>
        |      <tr><th>User:</th><td>{user_name}@{host_name}</td></tr>
        |      <tr><th>Working Directory:</th><td>{current_dir}</td></tr>
        |      <tr><th>Command Line:</th><td>{cmd_args}</td></tr>
        |      <tr><th>Clang Version:</th><td>{clang_version}</td></tr>
        |      <tr><th>Date:</th><td>{date}</td></tr>
        |    </table>""", indent).format(html_title=args.html_title,
                                         user_name=getpass.getuser(),
                                         host_name=socket.gethostname(),
                                         current_dir=prefix,
                                         cmd_args=' '.join(sys.argv),
                                         clang_version=get_version(args.clang),
                                         date=datetime.datetime.today(
                                         ).strftime('%c')))
        for fragment in fragments:
            # copy the content of fragments
            with open(fragment, 'r') as input_handle:
                shutil.copyfileobj(input_handle, handle)
        handle.write(reindent("""
        |  </body>
        |</html>""", indent))


def bug_summary(output_dir, bug_counter):
    """ Bug summary is a HTML table to give a better overview of the bugs. """

    name = os.path.join(output_dir, 'summary.html.fragment')
    with open(name, 'w') as handle:
        indent = 4
        handle.write(reindent("""
        |<h2>Bug Summary</h2>
        |<table>
        |  <thead>
        |    <tr>
        |      <td>Bug Type</td>
        |      <td>Quantity</td>
        |      <td class="sorttable_nosort">Display?</td>
        |    </tr>
        |  </thead>
        |  <tbody>""", indent))
        handle.write(reindent("""
        |    <tr style="font-weight:bold">
        |      <td class="SUMM_DESC">All Bugs</td>
        |      <td class="Q">{0}</td>
        |      <td>
        |        <center>
        |          <input checked type="checkbox" id="AllBugsCheck"
        |                 onClick="CopyCheckedStateToCheckButtons(this);"/>
        |        </center>
        |      </td>
        |    </tr>""", indent).format(bug_counter.total))
        for category, types in bug_counter.categories.items():
            handle.write(reindent("""
        |    <tr>
        |      <th>{0}</th><th colspan=2></th>
        |    </tr>""", indent).format(category))
            for bug_type in types.values():
                handle.write(reindent("""
        |    <tr>
        |      <td class="SUMM_DESC">{bug_type}</td>
        |      <td class="Q">{bug_count}</td>
        |      <td>
        |        <center>
        |          <input checked type="checkbox"
        |                 onClick="ToggleDisplay(this,'{bug_type_class}');"/>
        |        </center>
        |      </td>
        |    </tr>""", indent).format(**bug_type))
        handle.write(reindent("""
        |  </tbody>
        |</table>""", indent))
        handle.write(comment('SUMMARYBUGEND'))
    return name


def bug_report(output_dir, prefix):
    # type: (str, str) -> str
    """ Creates a fragment from the analyzer reports. """

    pretty = prettify_bug(prefix, output_dir)
    bugs = (pretty(bug) for bug in read_bugs(output_dir, True))

    name = os.path.join(output_dir, 'bugs.html.fragment')
    with open(name, 'w') as handle:
        indent = 4
        handle.write(reindent("""
        |<h2>Reports</h2>
        |<table class="sortable" style="table-layout:automatic">
        |  <thead>
        |    <tr>
        |      <td>Bug Group</td>
        |      <td class="sorttable_sorted">
        |        Bug Type
        |        <span id="sorttable_sortfwdind">&nbsp;&#x25BE;</span>
        |      </td>
        |      <td>File</td>
        |      <td>Function/Method</td>
        |      <td class="Q">Line</td>
        |      <td class="Q">Path Length</td>
        |      <td class="sorttable_nosort"></td>
        |    </tr>
        |  </thead>
        |  <tbody>""", indent))
        handle.write(comment('REPORTBUGCOL'))
        for current in bugs:
            handle.write(reindent("""
        |    <tr class="{bug_type_class}">
        |      <td class="DESC">{bug_category}</td>
        |      <td class="DESC">{bug_type}</td>
        |      <td>{bug_file}</td>
        |      <td class="DESC">{bug_function}</td>
        |      <td class="Q">{bug_line}</td>
        |      <td class="Q">{bug_path_length}</td>
        |      <td><a href="{report_file}#EndPath">View Report</a></td>
        |    </tr>""", indent).format(**current))
            handle.write(comment('REPORTBUG', {'id': current['report_file']}))
        handle.write(reindent("""
        |  </tbody>
        |</table>""", indent))
        handle.write(comment('REPORTBUGEND'))
    return name


def crash_report(output_dir, prefix):
    # type: (str, str) -> str
    """ Creates a fragment from the compiler crashes. """

    name = os.path.join(output_dir, 'crashes.html.fragment')
    with open(name, 'w') as handle:
        indent = 4
        handle.write(reindent("""
        |<h2>Analyzer Failures</h2>
        |<p>The analyzer had problems processing the following files:</p>
        |<table>
        |  <thead>
        |    <tr>
        |      <td>Problem</td>
        |      <td>Source File</td>
        |      <td>Preprocessed File</td>
        |      <td>STDERR Output</td>
        |    </tr>
        |  </thead>
        |  <tbody>""", indent))
        for crash in Crash.read(output_dir):
            current = crash.pretty(prefix, output_dir)
            handle.write(reindent("""
        |    <tr>
        |      <td>{problem}</td>
        |      <td>{source}</td>
        |      <td><a href="{file}">preprocessor output</a></td>
        |      <td><a href="{stderr}">analyzer std err</a></td>
        |    </tr>""", indent).format(**current))
            handle.write(comment('REPORTPROBLEM', current))
        handle.write(reindent("""
        |  </tbody>
        |</table>""", indent))
        handle.write(comment('REPORTCRASHES'))
    return name


class Crash:
    def __init__(self,
                 source,    # type: str
                 problem,   # type: str
                 file,      # type: str
                 info,      # type: str
                 stderr     # type: str
                 ):
        # type: (...) -> None
        self.source = source
        self.problem = problem
        self.file = file
        self.info = info
        self.stderr = stderr

    def pretty(self, prefix, output_dir):
        # type: (Crash, str, str) -> Dict[str, str]
        """ Make safe this values to embed into HTML. """

        return {
            'source':   escape(chop(prefix, self.source)),
            'problem':  escape(self.problem),
            'file':     escape(chop(output_dir, self.file)),
            'info':     escape(chop(output_dir, self.info)),
            'stderr':   escape(chop(output_dir, self.stderr))
        }

    @classmethod
    def _parse_info_file(cls, filename):
        # type: (str) -> Optional[Tuple[str, str]]
        """ Parse out the crash information from the report file. """

        lines = list(safe_readlines(filename))
        return None if len(lines) < 2 else (lines[0], lines[1])

    @classmethod
    def read(cls, output_dir):
        # type: (str) -> Iterator[Crash]
        """ Generate a unique sequence of crashes from given directory. """

        pattern = os.path.join(output_dir, 'failures', '*.info.txt')
        for info_filename in glob.iglob(pattern):
            base_filename = info_filename[0:-len('.info.txt')]
            stderr_filename = "{}.stderr.txt".format(base_filename)

            source_and_problem = cls._parse_info_file(info_filename)
            if source_and_problem is not None:
                yield Crash(
                    source=source_and_problem[0],
                    problem=source_and_problem[1],
                    file=base_filename,
                    info=info_filename,
                    stderr=stderr_filename)


class Bug:
    def __init__(self):
        # type: (...) -> None
        super().__init__()

    def __eq__(self, o):
        # type: (Bug, object) -> bool
        return super().__eq__(o)

    def __hash__(self):
        # type: (Bug) -> int
        return super().__hash__()

    def __format__(self, format_spec):
        # type: (Bug, str) -> str
        return super().__format__(format_spec)


def read_bugs(output_dir, html):
    # type: (str, bool) -> Generator[Dict[str, Any], None, None]
    """ Generate a unique sequence of bugs from given output directory.

    Duplicates can be in a project if the same module was compiled multiple
    times with different compiler options. These would be better to show in
    the final report (cover) only once. """

    def empty(file_name):
        return os.stat(file_name).st_size == 0

    hash_str = '{bug_line}.{bug_path_length}/{bug_type}:{bug_file}'
    duplicate = duplicate_check(lambda bug: hash_str.format(**bug))

    # get the right parser for the job.
    parser = parse_bug_html if html else parse_bug_plist
    # get the input files, which are not empty.
    pattern = os.path.join(output_dir, '*.html' if html else '*.plist')
    bug_files = (file for file in glob.iglob(pattern) if not empty(file))

    for bug_file in bug_files:
        for bug in parser(bug_file):
            if not duplicate(bug):
                yield bug


def parse_bug_plist(filename):
    # type: (str) -> Generator[Dict[str, Any], None, None]
    """ Returns the generator of bugs from a single .plist file. """

    content = plistlib.readPlist(filename)
    files = content.get('files', [])
    for bug in content.get('diagnostics', []):
        if len(files) <= int(bug['location']['file']):
            logging.warning('Parsing bug from "%s" failed', filename)
            continue

        yield {
            'result': filename,
            'bug_type': bug['type'],
            'bug_category': bug['category'],
            'bug_line': int(bug['location']['line']),
            'bug_path_length': int(bug['location']['col']),
            'bug_file': files[int(bug['location']['file'])]
        }


def parse_bug_html(filename):
    # type: (str) -> Generator[Dict[str, Any], None, None]
    """ Parse out the bug information from HTML output. """

    patterns = [re.compile(r'<!-- BUGTYPE (?P<bug_type>.*) -->$'),
                re.compile(r'<!-- BUGFILE (?P<bug_file>.*) -->$'),
                re.compile(r'<!-- BUGPATHLENGTH (?P<bug_path_length>.*) -->$'),
                re.compile(r'<!-- BUGLINE (?P<bug_line>.*) -->$'),
                re.compile(r'<!-- BUGCATEGORY (?P<bug_category>.*) -->$'),
                re.compile(r'<!-- BUGDESC (?P<bug_description>.*) -->$'),
                re.compile(r'<!-- FUNCTIONNAME (?P<bug_function>.*) -->$')]
    endsign = re.compile(r'<!-- BUGMETAEND -->')

    bug = {
        'report_file': filename,
        'bug_function': 'n/a',  # compatibility with < clang-3.5
        'bug_category': 'Other',
        'bug_line': 0,
        'bug_path_length': 1
    }

    for line in safe_readlines(filename):
        # do not read the file further
        if endsign.match(line):
            break
        # search for the right lines
        for regex in patterns:
            match = regex.match(line.strip())
            if match:
                bug.update(match.groupdict())
                break

    encode_value(bug, 'bug_line', int)
    encode_value(bug, 'bug_path_length', int)

    yield bug


def category_type_name(bug):
    # type: (Dict[str, Any]) -> str
    """ Create a new bug attribute from bug by category and type.

    The result will be used as CSS class selector in the final report. """

    def smash(key):
        # type: (str) -> str
        """ Make value ready to be HTML attribute value. """

        return bug.get(key, '').lower().replace(' ', '_').replace("'", '')

    return escape('bt_' + smash('bug_category') + '_' + smash('bug_type'))


def duplicate_check(hash_function):
    # type: (Callable[[Any], str]) -> Callable[[Dict[str, Any]], bool]
    """ Workaround to detect duplicate dictionary values.

    Python `dict` type has no `hash` method, which is required by the `set`
    type to store elements.

    This solution still not store the `dict` as value in a `set`. Instead
    it calculate a `string` hash and store that. Therefore it can only say
    that hash is already taken or not.

    This method is a factory method, which returns a predicate. """

    def predicate(entry):
        # type: (Dict[str, Any]) -> bool
        """ The predicate which calculates and stores the hash of the given
        entries. The entry type has to work with the given hash function.

        :param entry: the questioned entry,
        :return: true/false depends the hash value is already seen or not.
        """
        entry_hash = hash_function(entry)  # type: str
        if entry_hash not in state:
            state.add(entry_hash)
            return False
        return True

    state = set()  # type: Set[str]
    return predicate


def create_counters():
    # type () -> Callable[[Dict[str, Any]], None] FIXME
    """ Create counters for bug statistics.

    Two entries are maintained: 'total' is an integer, represents the
    number of bugs. The 'categories' is a two level categorisation of bug
    counters. The first level is 'bug category' the second is 'bug type'.
    Each entry in this classification is a dictionary of 'count', 'type'
    and 'label'. """

    def predicate(bug):
        # type (Dict[str, Any]) -> None FIXME
        bug_category = bug['bug_category']
        bug_type = bug['bug_type']
        current_category = predicate.categories.get(bug_category, dict())
        current_type = current_category.get(bug_type, {
            'bug_type': bug_type,
            'bug_type_class': category_type_name(bug),
            'bug_count': 0
        })
        current_type.update({'bug_count': current_type['bug_count'] + 1})
        current_category.update({bug_type: current_type})
        predicate.categories.update({bug_category: current_category})
        predicate.total += 1

    predicate.total = 0  # type: int
    predicate.categories = dict()  # type: Dict[str, Any]
    return predicate


def prettify_bug(prefix, output_dir):
    # type: (str, str) -> Callable[[Dict[str, Any]], Dict[str, str]]
    def predicate(bug):
        # type: (Dict[str, Any]) -> Dict[str, str]
        """ Make safe this values to embed into HTML. """

        bug['bug_type_class'] = category_type_name(bug)

        encode_value(bug, 'bug_file', lambda x: escape(chop(prefix, x)))
        encode_value(bug, 'bug_category', escape)
        encode_value(bug, 'bug_type', escape)
        encode_value(bug, 'report_file', lambda x: escape(chop(output_dir, x)))
        return bug

    return predicate


def copy_resource_files(output_dir):
    # type: (str) -> None
    """ Copy the javascript and css files to the report directory. """

    this_dir = os.path.dirname(os.path.realpath(__file__))
    for resource in os.listdir(os.path.join(this_dir, 'resources')):
        shutil.copy(os.path.join(this_dir, 'resources', resource), output_dir)


def safe_readlines(filename):
    # type: (str) -> Iterator[str]
    """ Read and return an iterator of lines from file. """

    with open(filename, mode='rb') as handler:
        for line in handler.readlines():
            # this is a workaround to fix windows read '\r\n' as new lines.
            yield line.decode(errors='ignore').rstrip()


def encode_value(container, key, encode):
    # type: (Dict[str, Any], str, Callable[[Any], Any]) -> None
    """ Run 'encode' on 'container[key]' value and update it. """

    if key in container:
        value = encode(container[key])
        container.update({key: value})


def chop(prefix, filename):
    # type: (str, str) -> str
    """ Create 'filename' from '/prefix/filename' """

    return filename if not prefix else os.path.relpath(filename, prefix)


def escape(text):
    # type: (str) -> str
    """ Paranoid HTML escape method. (Python version independent) """

    escape_table = {
        '&': '&amp;',
        '"': '&quot;',
        "'": '&apos;',
        '>': '&gt;',
        '<': '&lt;'
    }
    return ''.join(escape_table.get(c, c) for c in text)


def reindent(text, indent):
    # type: (str, int) -> str
    """ Utility function to format html output and keep indentation. """

    result = ''
    for line in text.splitlines():
        if line.strip():
            result += ' ' * indent + line.split('|')[1] + os.linesep
    return result


def comment(name, opts=None):
    # type: (str, Dict[str, str]) -> str
    """ Utility function to format meta information as comment. """

    attributes = ''
    if opts:
        for key, value in opts.items():
            attributes += ' {0}="{1}"'.format(key, value)

    return '<!-- {0}{1} -->{2}'.format(name, attributes, os.linesep)


def commonprefix_from(filename):
    # type: (str) -> str
    """ Create file prefix from a compilation database entries. """

    with open(filename, 'r') as handle:
        return commonprefix(item['file'] for item in json.load(handle))


def commonprefix(files):
    # type: (Iterator[str]) -> str
    """ Fixed version of os.path.commonprefix.

    :param files: list of file names.
    :return: the longest path prefix that is a prefix of all files. """
    result = None
    for current in files:
        if result is not None:
            result = os.path.commonprefix([result, current])
        else:
            result = current

    if result is None:
        return ''
    elif not os.path.isdir(result):
        return os.path.dirname(result)
    return os.path.abspath(result)
