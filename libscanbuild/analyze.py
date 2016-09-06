# -*- coding: utf-8 -*-
#                     The LLVM Compiler Infrastructure
#
# This file is distributed under the University of Illinois Open Source
# License. See LICENSE.TXT for details.
""" This module implements the 'scan-build' command API.

To run the static analyzer against a build is done in multiple steps:

 -- Intercept: capture the compilation command during the build,
 -- Analyze:   run the analyzer against the captured commands,
 -- Report:    create a cover report from the analyzer outputs.  """

import re
import os
import os.path
import json
import argparse
import logging
import multiprocessing
from libscanbuild import command_entry_point, wrapper_environment, \
    wrapper_entry_point, reconfigure_logging, tempdir, execute_and_report
from libscanbuild.runner import run, logging_analyzer_output
from libscanbuild.intercept import capture
from libscanbuild.report import report_directory, document
from libscanbuild.clang import get_checkers
from libscanbuild.compilation import split_command

__all__ = ['analyze_build_main', 'analyze_build_wrapper']

COMPILER_WRAPPER_CC = 'analyze-cc'
COMPILER_WRAPPER_CXX = 'analyze-c++'


@command_entry_point
def analyze_build_main(bin_dir, from_build_command):
    """ Entry point for 'analyze-build' and 'scan-build'. """

    parser = create_parser(from_build_command)
    args = parser.parse_args()

    reconfigure_logging(args.verbose)
    logging.debug('Parsed arguments: %s', args)

    validate(parser, args, from_build_command)

    with report_directory(args.output, args.keep_empty) as target_dir:
        exit_code = 0
        cleanup = (lambda: 0)
        if not from_build_command:
            # run the analyzer against a compilation db
            run_analyzer(args, target_dir)
        else:
            # run against a build command. there are cases, when analyzer run
            # is not required. but we need to set up everything for the
            # wrappers, because 'configure' needs to capture the CC/CXX values
            # for the Makefile.
            if args.intercept_first:
                # run build command with intercept module
                exit_code = capture(args, bin_dir)
                if need_analyzer(args.build):
                    # run the analyzer against the captured commands
                    run_analyzer(args, target_dir)
                cleanup = (lambda: os.unlink(args.cdb))
            else:
                # run build command and analyzer with compiler wrappers
                report_dir = target_dir if need_analyzer(args.build) else None
                environment = setup_environment(args, bin_dir, report_dir)
                exit_code = execute_and_report(args.build, env=environment)
        # cover report generation and bug counting
        number_of_bugs = document(args, target_dir)
        # do cleanup temporary files
        cleanup()
        # set exit status as it was requested
        return number_of_bugs if args.status_bugs else exit_code


def need_analyzer(args):
    """ Check the intent of the build command.

    When static analyzer run against project configure step, it should be
    silent and no need to run the analyzer or generate report.

    To run `scan-build` against the configure step might be necessary,
    when compiler wrappers are used. That's the moment when build setup
    check the compiler and capture the location for the build process. """

    return len(args) and not re.search('configure|autogen', args[0])


def run_analyzer(args, output_dir):
    """ Runs the analyzer against the given compilation database. """

    def exclude(filename):
        """ Return true when any excluded directory prefix the filename. """
        return any(re.match(r'^' + directory, filename)
                   for directory in args.excludes)

    consts = {
        'clang': args.clang,
        'output_dir': output_dir,
        'output_format': args.output_format,
        'output_failures': args.output_failures,
        'direct_args': analyzer_params(args),
        'force_debug': args.force_debug
    }

    logging.debug('run analyzer against compilation database')
    with open(args.cdb, 'r') as handle:
        generator = (dict(cmd, **consts)
                     for cmd in json.load(handle) if not exclude(cmd['file']))
        # when verbose output requested execute sequentially
        pool = multiprocessing.Pool(1 if args.verbose > 2 else None)
        for current in pool.imap_unordered(run, generator):
            logging_analyzer_output(current)
        pool.close()
        pool.join()


def setup_environment(args, bin_dir, destination):
    """ Set up environment for build command to interpose compiler wrapper. """

    environment = dict(os.environ)
    environment.update(
        wrapper_environment(
            c_wrapper=os.path.join(bin_dir, COMPILER_WRAPPER_CC),
            cxx_wrapper=os.path.join(bin_dir, COMPILER_WRAPPER_CXX),
            c_compiler=args.cc,
            cxx_compiler=args.cxx,
            verbose=args.verbose))
    # request analyzer run when destination directory is not None
    environment.update({
        'ANALYZE_BUILD_CLANG': args.clang,
        'ANALYZE_BUILD_REPORT_DIR': destination,
        'ANALYZE_BUILD_REPORT_FORMAT': args.output_format,
        'ANALYZE_BUILD_REPORT_FAILURES': 'yes' if args.output_failures else '',
        'ANALYZE_BUILD_PARAMETERS': ' '.join(analyzer_params(args)),
        'ANALYZE_BUILD_FORCE_DEBUG': 'yes' if args.force_debug else ''
    } if destination else {})
    return environment


@command_entry_point
@wrapper_entry_point
def analyze_build_wrapper(**kwargs):
    """ Entry point for `analyze-cc` and `analyze-c++` compiler wrappers. """

    # don't run analyzer when compilation fails. or when it's not requested.
    if kwargs['result'] or not os.getenv('ANALYZE_BUILD_CLANG'):
        return
    # don't run analyzer when the command is not a compilation
    # (can be preprocessing or a linking only execution of the compiler)
    compilation = split_command(kwargs['command'])
    if compilation is None:
        return
    # collect the needed parameters from environment, crash when missing
    env = os.environ.copy()
    parameters = {
        'clang': env['ANALYZE_BUILD_CLANG'],
        'output_dir': env['ANALYZE_BUILD_REPORT_DIR'],
        'output_format': env['ANALYZE_BUILD_REPORT_FORMAT'],
        'output_failures': env.get('ANALYZE_BUILD_REPORT_FAILURES', False),
        'direct_args': env.get('ANALYZE_BUILD_PARAMETERS', '').split(' '),
        'force_debug': env.get('ANALYZE_BUILD_FORCE_DEBUG', False),
        'directory': os.getcwd(),
        'command': [kwargs['compiler'], '-c'] + compilation.flags
    }
    # call static analyzer against the compilation
    for source in compilation.files:
        current = run(dict(parameters, file=source))
        # display error message from the static analyzer
        logging_analyzer_output(current)


def analyzer_params(args):
    """ A group of command line arguments can mapped to command
    line arguments of the analyzer. This method generates those. """

    def prefix_with(constant, pieces):
        """ From a sequence create another sequence where every second element
        is from the original sequence and the odd elements are the prefix.

        eg.: prefix_with(0, [1,2,3]) creates [0, 1, 0, 2, 0, 3] """

        return [elem for piece in pieces for elem in [constant, piece]]

    result = []

    if args.store_model:
        result.append('-analyzer-store={0}'.format(args.store_model))
    if args.constraints_model:
        result.append('-analyzer-constraints={0}'.format(
            args.constraints_model))
    if args.internal_stats:
        result.append('-analyzer-stats')
    if args.analyze_headers:
        result.append('-analyzer-opt-analyze-headers')
    if args.stats:
        result.append('-analyzer-checker=debug.Stats')
    if args.maxloop:
        result.extend(['-analyzer-max-loop', str(args.maxloop)])
    if args.output_format:
        result.append('-analyzer-output={0}'.format(args.output_format))
    if args.analyzer_config:
        result.append(args.analyzer_config)
    if args.verbose >= 4:
        result.append('-analyzer-display-progress')
    if args.plugins:
        result.extend(prefix_with('-load', args.plugins))
    if args.enable_checker:
        checkers = ','.join(args.enable_checker)
        result.extend(['-analyzer-checker', checkers])
    if args.disable_checker:
        checkers = ','.join(args.disable_checker)
        result.extend(['-analyzer-disable-checker', checkers])
    if os.getenv('UBIVIZ'):
        result.append('-analyzer-viz-egraph-ubigraph')

    return prefix_with('-Xclang', result)


def print_active_checkers(checkers):
    """ Print active checkers to stdout. """

    for name in sorted(name for name, (_, active) in checkers.items()
                       if active):
        print(name)


def print_checkers(checkers):
    """ Print verbose checker help to stdout. """

    print('')
    print('available checkers:')
    print('')
    for name in sorted(checkers.keys()):
        description, active = checkers[name]
        prefix = '+' if active else ' '
        if len(name) > 30:
            print(' {0} {1}'.format(prefix, name))
            print(' ' * 35 + description)
        else:
            print(' {0} {1: <30}  {2}'.format(prefix, name, description))
    print('')
    print('NOTE: "+" indicates that an analysis is enabled by default.')
    print('')


def validate(parser, args, from_build_command):
    """ Validation done by the parser itself, but semantic check still
    needs to be done. This method is doing that. """

    # Make plugins always a list. (It might be None when not specified.)
    args.plugins = args.plugins if args.plugins else []

    if args.help_checkers_verbose:
        print_checkers(get_checkers(args.clang, args.plugins))
        parser.exit()
    elif args.help_checkers:
        print_active_checkers(get_checkers(args.clang, args.plugins))
        parser.exit()

    if from_build_command and not args.build:
        parser.error('missing build command')


def create_parser(from_build_command):
    """ Command line argument parser factory method. """

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument(
        '--verbose', '-v',
        action='count',
        default=0,
        help="""Enable verbose output from '%(prog)s'. A second and third
                flag increases verbosity.""")
    parser.add_argument(
        '--override-compiler',
        action='store_true',
        help="""Always resort to the compiler wrapper even when better
                interposition methods are available.""")
    parser.add_argument(
        '--intercept-first',
        action='store_true',
        help="""Run the build commands only, build a compilation database,
                then run the static analyzer afterwards.
                Generally speaking it has better coverage on build commands.
                With '--override-compiler' it use compiler wrapper, but does
                not run the analyzer till the build is finished. """)
    parser.add_argument(
        '--cdb',
        metavar='<file>',
        default="compile_commands.json",
        help="""The JSON compilation database.""")

    parser.add_argument(
        '--output', '-o',
        metavar='<path>',
        default=tempdir(),
        help="""Specifies the output directory for analyzer reports.
                Subdirectory will be created if default directory is targeted.
                """)
    parser.add_argument(
        '--status-bugs',
        action='store_true',
        help="""By default, the exit status of '%(prog)s' is the same as the
                executed build command. Specifying this option causes the exit
                status of '%(prog)s' to be non zero if it found potential bugs
                and zero otherwise.""")
    parser.add_argument(
        '--html-title',
        metavar='<title>',
        help="""Specify the title used on generated HTML pages.
                If not specified, a default title will be used.""")
    parser.add_argument(
        '--analyze-headers',
        action='store_true',
        help="""Also analyze functions in #included files. By default, such
                functions are skipped unless they are called by functions
                within the main source file.""")
    format_group = parser.add_mutually_exclusive_group()
    format_group.add_argument(
        '--plist', '-plist',
        dest='output_format',
        const='plist',
        default='html',
        action='store_const',
        help="""This option outputs the results as a set of .plist files.""")
    format_group.add_argument(
        '--plist-html', '-plist-html',
        dest='output_format',
        const='plist-html',
        default='html',
        action='store_const',
        help="""This option outputs the results as a set of .html and .plist
                files.""")
    # TODO: implement '-view '

    advanced = parser.add_argument_group('advanced options')
    advanced.add_argument(
        '--keep-empty',
        action='store_true',
        help="""Don't remove the build results directory even if no issues
                were reported.""")
    advanced.add_argument(
        '--no-failure-reports', '-no-failure-reports',
        dest='output_failures',
        action='store_false',
        help="""Do not create a 'failures' subdirectory that includes analyzer
                crash reports and preprocessed source files.""")
    advanced.add_argument(
        '--stats', '-stats',
        action='store_true',
        help="""Generates visitation statistics for the project being analyzed.
                """)
    advanced.add_argument(
        '--internal-stats',
        action='store_true',
        help="""Generate internal analyzer statistics.""")
    advanced.add_argument(
        '--maxloop', '-maxloop',
        metavar='<loop count>',
        type=int,
        help="""Specifiy the number of times a block can be visited before
                giving up. Increase for more comprehensive coverage at a cost
                of speed.""")
    advanced.add_argument(
        '--store', '-store',
        metavar='<model>',
        dest='store_model',
        choices=['region', 'basic'],
        help="""Specify the store model used by the analyzer.
                'region' specifies a field- sensitive store model.
                'basic' which is far less precise but can more quickly
                analyze code. 'basic' was the default store model for
                checker-0.221 and earlier.""")
    advanced.add_argument(
        '--constraints', '-constraints',
        metavar='<model>',
        dest='constraints_model',
        choices=['range', 'basic'],
        help="""Specify the constraint engine used by the analyzer. Specifying
                'basic' uses a simpler, less powerful constraint model used by
                checker-0.160 and earlier.""")
    advanced.add_argument(
        '--use-analyzer',
        metavar='<path>',
        dest='clang',
        default='clang',
        help="""'%(prog)s' uses the 'clang' executable relative to itself for
                static analysis. One can override this behavior with this
                option by using the 'clang' packaged with Xcode (on OS X) or
                from the PATH.""")
    advanced.add_argument(
        '--use-cc',
        metavar='<path>',
        dest='cc',
        default=os.getenv('CC', 'cc'),
        help="""When '%(prog)s' analyzes a project by interposing a "fake
                compiler", which executes a real compiler for compilation and
                do other tasks (to run the static analyzer or just record the
                compiler invocation). Because of this interposing, '%(prog)s'
                does not know what compiler your project normally uses.
                Instead, it simply overrides the CC environment variable, and
                guesses your default compiler.

                If you need '%(prog)s' to use a specific compiler for
                *compilation* then you can use this option to specify a path
                to that compiler.""")
    advanced.add_argument(
        '--use-c++',
        metavar='<path>',
        dest='cxx',
        default=os.getenv('CXX', 'c++'),
        help="""This is the same as "--use-cc" but for C++ code.""")
    advanced.add_argument(
        '--analyzer-config', '-analyzer-config',
        metavar='<options>',
        help="""Provide options to pass through to the analyzer's
                -analyzer-config flag. Several options are separated with
                comma: 'key1=val1,key2=val2'

                Available options:
                    stable-report-filename=true or false (default)

                Switch the page naming to:
                report-<filename>-<function/method name>-<id>.html
                instead of report-XXXXXX.html""")
    advanced.add_argument(
        '--exclude',
        metavar='<directory>',
        dest='excludes',
        action='append',
        default=[],
        help="""Do not run static analyzer against files found in this
                directory. (You can specify this option multiple times.)
                Could be useful when project contains 3rd party libraries.
                The directory path shall be absolute path as file names in
                the compilation database.""")
    advanced.add_argument(
        '--force-analyze-debug-code',
        dest='force_debug',
        action='store_true',
        help="""Tells analyzer to enable assertions in code even if they were
                disabled during compilation, enabling more precise results.""")

    plugins = parser.add_argument_group('checker options')
    plugins.add_argument(
        '--load-plugin', '-load-plugin',
        metavar='<plugin library>',
        dest='plugins',
        action='append',
        help="""Loading external checkers using the clang plugin interface.""")
    plugins.add_argument(
        '--enable-checker', '-enable-checker',
        metavar='<checker name>',
        action=AppendCommaSeparated,
        help="""Enable specific checker.""")
    plugins.add_argument(
        '--disable-checker', '-disable-checker',
        metavar='<checker name>',
        action=AppendCommaSeparated,
        help="""Disable specific checker.""")
    plugins.add_argument(
        '--help-checkers',
        action='store_true',
        help="""A default group of checkers is run unless explicitly disabled.
                Exactly which checkers constitute the default group is a
                function of the operating system in use. These can be printed
                with this flag.""")
    plugins.add_argument(
        '--help-checkers-verbose',
        action='store_true',
        help="""Print all available checkers and mark the enabled ones.""")

    if from_build_command:
        parser.add_argument(
            dest='build',
            nargs=argparse.REMAINDER,
            help="""Command to run.""")

    return parser


class AppendCommaSeparated(argparse.Action):
    """ argparse Action class to support multiple comma separated lists. """

    def __call__(self, __parser, namespace, values, __option_string):
        # getattr(obj, attr, default) does not really returns default but none
        if getattr(namespace, self.dest, None) is None:
            setattr(namespace, self.dest, [])
        # once it's fixed we can use as expected
        actual = getattr(namespace, self.dest)
        actual.extend(values.split(','))
        setattr(namespace, self.dest, actual)
