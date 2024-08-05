#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse
import configparser
import os
import re
from collections import defaultdict, namedtuple
from datetime import datetime, timedelta

import arrow
import orgparse
import pytz
from jira import JIRA

Interval = namedtuple('Interval', ['start', 'end'])


def match_lookup_intervals(intervals, clock_start, clock_end):
    """
    If either clock_start or clock end is in our matched intervals,
    we should return interval that matches
    """
    for ivl in intervals:
        if clock_start >= ivl.start and clock_start < ivl.end:
            # Clock start matches one of specified intervals, its our candidate
            end = clock_end if clock_end < ivl.end else ivl.end
            # cut the tail if it spans over ^^^ the interval!
            return Interval(clock_start, end)
    return None


def find_issue_in_heading(node, settings):
    """
    Find issue in heading by using project name regexp
    """
    heading = node.heading
    print('⇥ find_issue_in_heading')
    for check_re in settings['project_regexps']:
        for match in re.findall(check_re, heading):
            print('👌 Found! ', match)
            return match
    return None


def find_issue_in_property(node, settings):
    """
    Find issue in property `jira-task`.
    It takes precendence over every other method.
    """
    if not hasattr(node, 'properties'):
        return None
    print('⇥ find_issue_in_property')
    return node.properties.get('jira-task')


def find_issue_in_parent(node, settings):
    """
    Lookup for issue in parent spefication (if parent exists)
    """
    node = node.parent
    print('⇥ find_issue_in_parent')
    if node and hasattr(node, 'heading'):
        return find_jira_issue(node, settings)
    return None


def find_issue_by_tag(node, settings):
    """
    Find issue by tag of this task.
    """
    tags = node.tags
    print('⇥ find_issue_by_tag', tags)
    for tag in tags:
        if tag in settings['tags']:
            return settings['tags'][tag]


def find_jira_issue(node, settings):
    """
    Check it in Title (regexp on `project-id`)
    Check it in PROPERTY jira-task
    Lookup same in parent issue
    Return None of no issue found
    """
    # print("Tring to find out issue № for ", node.heading)
    print('↬ find_jira_issue: ', node.heading)
    jira_issue = (
        find_issue_in_property(node, settings) or
        find_issue_in_heading(node, settings) or
        find_issue_by_tag(node, settings) or
        find_issue_in_parent(node, settings)
    )
    return jira_issue


def send_interval_to_jira(jira_issue, interval, description, query_jira, send_data):
    """
    Find out if specified interval not on Jira server and send it
    """
    issue = None
    if query_jira:
        issue = jira.issue(jira_issue)
        for worklog in jira.worklogs(issue):
            worklog_started = arrow.get(worklog.started).datetime
            interval_start_utc = pytz.utc.localize(interval.start)
            if worklog_started == interval_start_utc:
                print("⛔ Not Sending to Jira: already submitted.", jira_issue, interval)
                return False
    if send_data and (interval.end-interval.start).seconds:
        if not issue:
            issue = jira.issue(jira_issue)
        if (interval.end-interval.start).seconds:
            jira.add_worklog(
                issue=jira_issue,
                started=interval.start,
                timeSpentSeconds=(interval.end-interval.start).seconds,
                comment=description
            )
            print("Inverval %s sent to Jira %s: (%s)" % (interval, jira_issue, description))
            return True
        print('⛔ Skipping (zero length) interval', jira_issue, interval)
    return False


def send_data_to_jira(args, settings):
    """
    Send data to Jira (if it was not already sent)
    """
    invoice_time = timedelta(0)
    skipped_time = timedelta(0)
    cumulative_time = timedelta(0)
    per_day = defaultdict(
        lambda: {
            'invoice_time': timedelta(0),
            'skipped_time': timedelta(0),
            'cumulative_time': timedelta(0),
            'added_time': timedelta(0)
        }
    )
    per_task = defaultdict(lambda: timedelta(0))

    for path in settings['org files']:
        orgfile = os.path.expanduser(path)
        root = orgparse.load(orgfile)
        for node in root[1:]:
            skip = False
            if node.properties.get('jira-skip'):
                skip = True
                print('⛔ Skipping by jira-skip: %s' % node.heading)
            if not node.clock:
                # print('No clock in %s, skipping' % node.heading)
                continue
            for clocked in node.clock:
                matched_interval = match_lookup_intervals(
                    args.intervals,
                    clocked.start, clocked.end
                )
                if matched_interval:
                    print(clocked.start, clocked.end)
                    if skip:
                        skipped_time += clocked.duration
                        per_day[clocked.start.date()]['skipped_time'] += clocked.duration
                    else:
                        invoice_time += clocked.duration
                        per_day[clocked.start.date()]['invoice_time'] += clocked.duration
                    per_day[clocked.start.date()]['cumulative_time'] += clocked.duration
                    cumulative_time += clocked.duration
                    jira_issue = find_jira_issue(node, settings)
                    per_task[jira_issue] += clocked.duration
                    if jira_issue and not skip:
                        added = send_interval_to_jira(
                            jira_issue, matched_interval, node.heading, args.query_jira, args.send_data_to_jira
                        )
                        if added:
                            per_day[clocked.start.date()]['added_time'] += clocked.duration
                    print('issue: %s\n' % jira_issue)
    print('* Per day\n\n| Date       | Total    | Invoiced  | Skipped |  Added |')
    for day in sorted(per_day):
        print(
            '|', '%s-%02d-%02d' % (day.year, day.month, day.day), '|',
            "%8s" % per_day[day]['cumulative_time'], '|',
            "%8s" % per_day[day]['invoice_time'], '|',
            "%8s" % per_day[day]['skipped_time'], '|'
            "%8s" % per_day[day]['added_time'], '|'
        )
    print('| |' + '|'.join([str(cumulative_time), str(invoice_time), str(skipped_time)]) + '| |')

    print('\n* Per task\n\n| Task       | Time    |')
    for task in per_task:
        print(
            '| %12s |' % task,
            ' %8s |' % per_task[task]
        )


class parseIntervals(argparse.Action):
    """
    Parse string intervals from YYYY-MM-DD..YYYY-MM-DD
    into `datetime` object.
    """
    def __call__(self, parser, args, values, option_string=None):
        intervals = [
            Interval(
                datetime(year=int(v[0:4]), month=int(v[5:7]), day=int(v[8:10])),
                datetime(year=int(v[12:16]), month=int(v[17:19]), day=int(v[20:22])))
            for v in values
        ]
        setattr(args, self.dest, intervals)


def read_settings():
    """parse config.ini syntax into more useful dict structure"""
    mysettings = {}
    mypath = os.path.dirname(__file__)
    configpath = os.path.join(mypath, 'config.ini')
    parser = configparser.RawConfigParser(allow_no_value=True)
    parser.optionxform = lambda option: option
    parser.read(configpath)
    # dict like
    for section in ['tags', 'global']:
        if section not in parser:
            continue
        mysettings[section] = {}
        for key in parser[section].keys():
            mysettings[section][key] = parser[section][key]
    # lists
    for section in ['project keys', 'org files']:
        if section not in parser:
            continue
        mysettings[section] = []
        for key in parser[section].keys():
            mysettings[section].append(key)
    return mysettings


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Send org-mode timeline to Jira.')
    parser.add_argument(
        'intervals', nargs="+",
        action=parseIntervals,
        help='From,To interval dates in format: YYYY-MM-DD..YYYY-MM-DD'
    )
    parser.add_argument(
        '--dont-send',
        dest='send_data_to_jira',
        default=True, action='store_false',
        help=''
    )
    parser.add_argument(
        '--dont-query',
        default=True, action='store_false',
        dest='query_jira',
        help='Do not query Jira'
    )

    args = parser.parse_args()
    settings = read_settings()
    from pprint import pprint
    pprint(settings)

    # JIRA
    JIRA_OPTIONS = {"server": settings['global']['server']}

    jira = JIRA(options=JIRA_OPTIONS, basic_auth=(settings['global']['email'], os.getenv('JIRA_TOKEN') or ''))

    project_keys = settings.get('project keys', '')
    if not project_keys:
        project_keys = jira.projects()   # FIXME this always returns the empty list and so unusable!

    settings['project_regexps'] = [re.compile(r'\s*({project}-\d+)\s*'.format(project=project)) for project in project_keys]

    send_data_to_jira(args, settings)
