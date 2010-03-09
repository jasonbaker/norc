#!/usr/bin/python

#
# Copyright (c) 2009, Perpetually.com, LLC.
# All rights reserved.
# 
# Redistribution and use in source and binary forms, with or without modification, 
# are permitted provided that the following conditions are met:
# 
#     * Redistributions of source code must retain the above copyright notice, 
#       this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright notice, 
#       this list of conditions and the following disclaimer in the documentation 
#       and/or other materials provided with the distribution.
#     * Neither the name of the Perpetually.com, LLC. nor the names of its 
#       contributors may be used to endorse or promote products derived from 
#       this software without specific prior written permission.
#     * 
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND 
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED 
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. 
# IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, 
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT 
# NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR 
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, 
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) 
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE 
# POSSIBILITY OF SUCH DAMAGE.
#



############################################
#
#
# The Norc daemon:
# Polls Norc for tasks to run, and runs them.
# Each Task is run in a seperate thread
#
#
# TODO:
#  - Eliminate the delay between a 
#    task becoming ready to run and running it.
#    - This could be achieved if the daemon was listening on a port
#    - Does MySQL support listening for DB events?
#  - max daemons per machine or dont bother?
#  - errors/message that occur in the daemon in the task thread are logged to the task, rather than the daemon.
#
#
#Darrell
#04/13/2009
############################################

import sys, os, time
import signal
import traceback

import threading# if using thread for running Tasks
import subprocess# if using forking for running Tasks

from optparse import OptionParser

from norc.core import models as tms_models
from norc.core import manage as tms_manage
from django.conf import settings

from norc.utils import log
log = log.Log(settings.LOGGING_DEBUG)


class RunnableTask(object):
    """
    Abstract interface for a running Task
    """
    
    __task__ = None
    __iteration__ = None
    __daemon_status__ = None
    
    def __init__(self, task, iteration, daemon_status):
        self.__task__ = task
        self.__iteration__ = iteration
        self.__daemon_status__ = daemon_status
    
    def get_task(self):
        return self.__task__
    def get_iteration(self):
        return self.__iteration__
    def get_daemon_status(self):
        return self.__daemon_status__
    
    def run(self):
        """Run this task!"""
        raise NotImplementedError
    def interrupt(self):
        """Interrupt this Task"""
        raise NotImplementedError

class ThreadedTaskLogger(object):
    """
    Direct output from each threaded task to the appropriate log file
    instead of mixed with the daemon or other Tasks.
    This must replace sys.stdout &| sys.stderr to be of any use.
    
    This thread was inspirational: http://mail.python.org/pipermail/python-list/2000-June/041632.html
    """
    
    __orig_stdout__ = None
    __orig_stderr__ = None
    __log_dir__ = None
    daemon_id_for_log = None
    buffer_data = None
    open_files = None
    
    def __init__(self, log_dir, daemon_id_for_log, buffer_data):
        if not os.path.exists(log_dir):
            raise Exception("log_dir '%s' not found!" % (log_dir))
        self.__log_dir__ = log_dir
        self.daemon_id_for_log = daemon_id_for_log
        if not daemon_id_for_log == None:
            log.info("TMSD stderr & stdout will be in '%s'" \
                % (self.__get_daemon_log_file_name__()))
        self.buffer_data = buffer_data
        self.open_files = {}
    
    def __get_daemon_log_file_name__(self):
        assert not self.daemon_id_for_log == None, "daemon_id_for_log is None! BUG!"
        fp = "%s/_tmsd/tmsd.%s" % (self.__log_dir__, self.daemon_id_for_log)
        return fp
    def __get_log_file__(self, fp):
        if not self.open_files.has_key(fp):
            if not os.path.exists(os.path.dirname(fp)):
                os.mkdir(os.path.dirname(fp))
            self.open_files[fp] = open(fp, 'a')
        return self.open_files[fp]
    def write_to_task_log(self, task, data):
        fh = self.__get_log_file__(task.get_log_file())
        fh.write(data)
        if not self.buffer_data:
            fh.flush()
    def write_to_daemon_log(self, data):
        fh = self.__get_log_file__(self.__get_daemon_log_file_name__())
        fh.write(data)
        if not self.buffer_data:
            fh.flush()
    def write(self, data):
        try:
            current_thread = threading.currentThread()
            if type(current_thread) == TaskInThread:
                self.write_to_task_log(current_thread.get_task(), data)
            elif not self.daemon_id_for_log == None:
                self.write_to_daemon_log(data)
            else:
                # all messages for daemon are sent to stdout
                self.__orig_stdout__.write(data)
                if not self.buffer_data:
                    self.__orig_stdout__.flush()
        except Exception, e:
            try:
                if type(current_thread) == TaskInThread:
                    self.__orig_stderr__.write("Exception occured writing log for Task id:%s \"%s\". BAD!\n" 
                        % (current_thread.get_task().get_id(), current_thread.get_task()))
                else:
                    self.__orig_stderr__.write("Exception occured writing log: '%s'. BAD!\n" 
                        % (e))
            except:
                self.__orig_stderr__.write("Exception occured writing log and couldn't determine task. BAD!\n")
        except:
            pass
    
    def close_log(self, task):
        fp = task.get_log_file()
        if self.open_files.has_key(fp):
            try:
                self.open_files[fp].close()
            except:
                pass
            try:
                del self.open_files[fp]
            except:
                pass
    def close_all(self):
        to_close = self.open_files.values()
        log.debug("Closing %s log file(s)" % (len(to_close)))
        for fh in to_close:
            fh.close()
    
    def start_redirect(self):
        self.__orig_stdout__ = sys.stdout
        self.__orig_stderr__ = sys.stderr
        sys.stdout = self
        sys.stderr = self
    def stop_redirect(self):
        self.close_all()
        sys.stdout = self.__orig_stdout__
        sys.stderr = self.__orig_stderr__

class NorcDaemon(object):
    """
    Abstract daemon; subclasses implement the running of the Task
    """
    
    __poll_frequency__ = None
    __daemon_status__ = None
    __break_tasks_to_run_loop__ = False
    
    def __init__(self, region, poll_frequency):
        self.__poll_frequency__ = poll_frequency
        self.__daemon_status__ = tms_models.NorcDaemonStatus.create(region)
    
    def get_poll_frequency(self):
        return self.__poll_frequency__
    def get_daemon_status(self):
        return self.__daemon_status__
    def __set_daemon_status__(self, daemon_status):
        self.__daemon_status__ = daemon_status
    
    def __do_run__(self):
        """Main daemon loop"""
        log.info("%s %s..." % (self.get_name(), str(self.get_daemon_status())))
        if settings.DEBUG:
            log.info("WARNING: settings.DEBUG is True: daemon will gobble up memory b/c django stores SQL queries.")
        self.get_daemon_status().set_status(tms_models.NorcDaemonStatus.STATUS_RUNNING)
        last_status = self.get_daemon_status().get_status()
        while True:
            if not last_status == self.get_daemon_status().get_status():
                log.info("tmsd state changed: %s -> %s" % (last_status, self.get_daemon_status().get_status()))
                last_status = self.get_daemon_status().get_status()
            self.__set_daemon_status__(self.get_daemon_status().thwart_cache())# see note in this method definition
            if self.get_daemon_status().is_stop_requested() or self.get_daemon_status().is_being_stopped():
                # don't kick off more tasks, but wait for those running to finish on their own
                self.get_daemon_status().set_status(tms_models.NorcDaemonStatus.STATUS_STOPINPROGRESS)
                num_running_tasks = self.get_num_running_tasks()
                if num_running_tasks == 0:
                    log.info("tmsd stop requested and no more tasks. Ending gracefully.")
                    self.get_daemon_status().set_status(tms_models.NorcDaemonStatus.STATUS_ENDEDGRACEFULLY)
                    return True
                else:
                    log.info("tmsd stop requested; waiting for %s task(s) to finish." % (num_running_tasks))
            elif self.get_daemon_status().is_kill_requested() or self.get_daemon_status().is_being_killed():
                running_tasks = self.get_running_tasks()
                if len(running_tasks) == 0:
                    log.info("tmsd kill requested but no tasks running. Ending gracefully.")
                    self.get_daemon_status().set_status(tms_models.NorcDaemonStatus.STATUS_ENDEDGRACEFULLY)
                    return True
                else:
                    log.info("tmsd kill requested; interrupting %s task(s) and stopping immediately." % (len(running_tasks)))
                    self.get_daemon_status().set_status(tms_models.NorcDaemonStatus.STATUS_KILLINPROGRESS)
                    for running_task in running_tasks:
                        # There's no way to actually interrupt python threads
                        # mark the task as ended in error, and leave it up to
                        # main() to call SIGKILL on this process.
                        log.info("interrupting task '%s'." % (running_task), indent_chars=4)
                        try:
                            running_task.interrupt()
                        except Exception, e:
                            log.error("Could not interrupt Task '%s'" % (running_task), e)
                    self.get_daemon_status().set_status(tms_models.NorcDaemonStatus.STATUS_KILLED)
                    return False
            elif self.get_daemon_status().is_pause_requested():
                log.info("tmsd pause requested.  Will just sit here.")
                self.get_daemon_status().set_status(tms_models.NorcDaemonStatus.STATUS_PAUSED)
            elif self.get_daemon_status().is_paused():
                log.debug("tmsd paused.  Just sittin' here.")
            elif self.get_daemon_status().is_running():
                self.__break_tasks_to_run_loop__ = False# We're definitely running; don't break unless told to now.
                self.run_batch()
            else:
                raise Exception("Don't know how to handle daemon state '%s'" % (self.tmsd_status.get_status()))
            # wait here before polling again
            time.sleep(self.get_poll_frequency())
        raise Exception("The main loop exited somehow without throwing an error. Bug?")
    
    def run_batch(self):
        tasks_to_run = tms_manage.get_tasks_allowed_to_run(end_completed_iterations=True, max_to_return=10)
        num_running_tasks = self.get_num_running_tasks()
        log.debug("tmsd running %s task(s), at least %s task(s) due to run" % (num_running_tasks, len(tasks_to_run)))
        need_resource_types = []
        for (task, iteration) in tasks_to_run:
            if self.__break_tasks_to_run_loop__:
                # some other thread (request_stop) doesn't want me to continue.  Stop here.
                break
            # check that there are currently sufficient resources to prevent
            # erroneously thinking this task can be run when it cannot.
            # There will be occasional cases where race conditions mean a task is not run when
            # it could be, but there are many more cases when this will save threads.
            if type(task) in need_resource_types:
                # A Task of this type already returned unavailable resources; don't check again.
                # This should be an efficiency gain for the running of Tasks to prevent 
                # excessive polling of the resources table when there are likely no new resources.
                #log.info("Assuming no resources avail for Task type '%s'" % (type(task)))
                pass
            elif task.resources_available_to_run(self.get_daemon_status().get_region()):
                try:
                    self.start_task(task, iteration)
                except Exception, e:
                    log.error("Could not run Task '%s'" % (task), e)
            else:
                need_resource_types.append(type(task))
    
    def run(self):
        """Start this daemon"""
        try:
            ended_gracefully = self.__do_run__()
            return ended_gracefully
        except Exception, e:
            self.get_daemon_status().set_status(tms_models.NorcDaemonStatus.STATUS_ERROR)
            log.error("tmsd suffered an internal error. BAD!", e)
            return False
    
    def request_stop(self):
        log.info("tmsd Sending stop request...")
        self.get_daemon_status().set_status(tms_models.NorcDaemonStatus.STATUS_STOPREQUESTED)
        self.__break_tasks_to_run_loop__ = True
    def request_kill(self):
        log.info("tmsd Sending kill request...")
        self.get_daemon_status().set_status(tms_models.NorcDaemonStatus.STATUS_KILLREQUESTED)
        self.__break_tasks_to_run_loop__ = True
    
    def get_num_running_tasks(self):
        """Return the number of currently running Tasks for this daemon"""
        return len(self.get_running_tasks())
    def get_name(self):
        """Return a name for this daemon implementation"""
        raise NotImplementedError
    def get_running_tasks(self):
        """Returns list of currently running RunnableTask's"""
        raise NotImplementedError
    def start_task(self, task, iteration):
        """Start the given Task in the given Iteration"""
        raise NotImplementedError



class TaskInProcess(RunnableTask):
    
    RUN_TASK_EXE = 'tmsd_run_task'
    
    __log_dir__ = None
    __subprocess__ = None
    
    def __init__(self, task, iteration, daemon_status, log_dir):
        RunnableTask.__init__(self, task, iteration, daemon_status)
        self.__log_dir__ = log_dir
    
    def run(self):
        #log.info("Starting Task \"%s\" in new process" % (self.get_task().get_name()))
        log_file_name = self.get_task().get_log_file()
        # TODO change this to get log file in RUN_TASK_EXE
        cmd = [TaskInProcess.RUN_TASK_EXE
            , "--daemon_status_id", str(self.get_daemon_status().get_id())
            , "--iteration_id", str(self.get_iteration().get_id())
            , "--task_library", str(self.get_task().get_library_name())
            , "--task_id", str(self.get_task().get_id())
            , "--stdout", log_file_name
            , "--stderr", "STDOUT"
        ]
        if log.get_logging_debug():
            cmd.append("--debug")
        if not os.path.exists(os.path.dirname(log_file_name)):
            os.mkdir(os.path.dirname(log_file_name))
        self.__subprocess__ = subprocess.Popen(cmd)
        # give the Task a chance to start; 
        # this prevents lots of false starts due to unavailable resources
        # that only are only unavailable to future tasks once this task has kicked off.
        time.sleep(2)
    
    def is_running(self):
        if self.__subprocess__ == None:
            # not even started yet
            return False
        return self.__subprocess__.poll() == None
    def get_exit_status(self):
        if self.__subprocess__ == None:
            # not even started yet
            return None
        return self.__subprocess__.returncode
    def get_pid(self):
        return self.__subprocess__.pid
    
    def interrupt(self):
        """Interrupt this Task"""
        assert not self.__subprocess__ == None, "Cannot interrupt process not started"
        # A bit of interpretive dance to get this to replicate what's much easier in the 2.6 version
        if self.is_running():
            # task is still running; interrupt it! 
            # TODO kill it? (would be signal.SIGKILL)
            log.info("sending SIGINT to pid:%s, task:%s" % (self.get_pid(), self.get_task().get_id()))
            os.kill(self.get_pid(), signal.SIGINT)
        elif self.get_exit_status():
            raise Exception("Task cannot be interrupted. It has already succeeded.")
        else:
            raise Exception("Task cannot be interrupted. It has failed with status %s." % (self.get_exit_status()))

class ForkingNorcDaemon(NorcDaemon):
    
    __log_dir__ = None
    __running_tasks__ = None
    
    def __init__(self, region, poll_frequency, log_dir, redirect_daemon_log):
        #import subprocess
        NorcDaemon.__init__(self, region, poll_frequency)
        self.__log_dir__ = log_dir
        self.__running_tasks__ = []
        daemon_id_for_log = None
        if redirect_daemon_log:
            daemon_id_for_log = self.get_daemon_status().get_id()
        self.__logger__ = ThreadedTaskLogger(log_dir, daemon_id_for_log, False)# only daemon output; don't buffer
    
    def get_name(self):
        """Return a name for this daemon implementation"""
        return 'TMSD (forking)'
    def __add_running_task__(self, running):
        self.__running_tasks__.append(running)
    def __get_task_label__(self, running_task):
        return "%s:%s" % (running_task.get_task().get_job(), running_task.get_task().get_name())
    def get_running_tasks(self):
        """Returns list of currently running RunnableTask's"""
        running_tasks = []
        to_cleanup = []
        for running_task in self.__running_tasks__:
            if running_task.is_running():
                running_tasks.append(running_task)
            else:
                to_cleanup.append(running_task)
                # no longer running; log that fact for convenience
                exit_status = running_task.get_exit_status()
                if exit_status == 0:
                    log.info("\"%s\" succeeded" % (self.__get_task_label__(running_task)))
                elif exit_status == 130:
                    log.info("\"%s\" timed out." % (self.__get_task_label__(running_task)))
                elif exit_status == 131:
                    log.info("\"%s\" was interrupted." % (self.__get_task_label__(running_task)))
                elif exit_status == 132:
                    log.info("\"%s\" was killed." % (self.__get_task_label__(running_task)))
                elif exit_status == 133:
                    log.info("\"%s\" did not run." % (self.__get_task_label__(running_task)))
                elif exit_status == 134:
                    log.info("\"%s\" ended without a status." % (self.__get_task_label__(running_task)))
                elif exit_status == 127:
                    raise Exception("\"%s\" failed b/c of internal error.  \
TaskInProcess.RUN_TASK_EXE '%s' could not be found! BAD!" % (self.__get_task_label__(running_task) \
                                , TaskInProcess.RUN_TASK_EXE))
                elif exit_status == 126:
                    raise Exception("\"%s\" failed b/c of internal error.  \
TaskInProcess.RUN_TASK_EXE '%s' is not executable! BAD!" % (self.__get_task_label__(running_task) \
                                , TaskInProcess.RUN_TASK_EXE))
                else:
                    log.info("\"%s\" failed with exit status %s!" % (self.__get_task_label__(running_task) \
                        , exit_status))
        
        for no_longer_running in to_cleanup:# TODO can this be done in one loop?
            self.__running_tasks__.remove(no_longer_running)
        
        return running_tasks
    def start_task(self, task, iteration):
        log.info("\"%s:%s\" starting in new process" % (task.get_job().get_name(), task.get_name()))
        tp = TaskInProcess(task, iteration, self.get_daemon_status(), self.__log_dir__)
        tp.run()
        self.__add_running_task__(tp)
    
    def run(self):
        try:
            self.__logger__.start_redirect()
            ended_gracefully = NorcDaemon.run(self)
            self.__logger__.stop_redirect()
            return ended_gracefully
        except Exception, e:
            log.error("Error running daemon!", e)
            return False
        except:
            log.error("Error running daemon & it was poorly thrown!", e)
            return False

#
#
#

class TaskInThread(RunnableTask, threading.Thread):
    
    __logger__ = None
    
    def __init__(self, task, iteration, daemon_status, logger):
        self.__logger__ = logger
        RunnableTask.__init__(self, task, iteration, daemon_status)
        threading.Thread.__init__(self)
    
    def run(self):
        try:
            try:
                self.get_task().do_run(self.get_iteration(), self.get_daemon_status())
            except Exception, e:
                log.error("Exception propegated from task.do_run(). BAD! Bug?", e)
            except:
                log.error("Poorly thrown exception propegated from task.do_run(). BAD! Bug?")
                traceback.print_exc()
        finally:
            self.__logger__.close_log(self.get_task())# TODO this feels hacky!
    
    def interrupt(self):
        """
        Cannot interrupt the Task thread, but can set it as ended on error.
        (Man, I wish I could interrupt threads in Python!)
        """
        self.get_task().set_ended_on_error(self.get_iteration(), self.get_daemon_status().get_region())
        # a small hack to log in the correct format, but whatever.
        msg = log.__format_msg__("ERROR", "Task was interrupted by the daemon! Sorry.\n", False, 0)
        self.__logger__.write_to_task_log(self.get_task(), msg)

class ThreadingNorcDaemon(NorcDaemon):
    
    __logger__ = None
    
    def __init__(self, region, poll_frequency, log_dir, redirect_daemon_log):
        NorcDaemon.__init__(self, region, poll_frequency)
        daemon_id_for_log = None
        if redirect_daemon_log:
            daemon_id_for_log = self.get_daemon_status().get_id()
        self.__logger__ = ThreadedTaskLogger(log_dir, daemon_id_for_log, True)
    
    def get_name(self):
        """Return a name for this daemon implementation"""
        return 'TMSD (threading)'
    def get_running_tasks(self):
        """Returns list of currently running RunnableTask's"""
        task_threads = []
        for a_thread in threading.enumerate():
            # this list includes all threads including this one; filter it.
            if type(a_thread) == TaskInThread:
                task_threads.append(a_thread)
        return task_threads
    
    def start_task(self, task, iteration):
        """Start the given Task in the given Iteration"""
        log.info("\"%s:%s\" starting in new thread" % (task.get_job().get_name(), task.get_name()))
        tt = TaskInThread(task, iteration, self.get_daemon_status(), sys.stdout)
        tt.start()
    
    def run(self):
        try:
            self.__logger__.start_redirect()
            ended_gracefully = NorcDaemon.run(self)
            self.__logger__.stop_redirect()
            return ended_gracefully
        except Exception, e:
            log.error("Error running daemon!", e)
            return False
        except:
            log.error("Error running daemon & it was poorly thrown!", e)
            return False

#
# Main
#

def main():
    parser = OptionParser("%prog --region <regionname> [--poll_frequency 3] [--threads] [--no_log_redirect] [--debug]")
    parser.add_option("--poll_frequency", action="store", default=3, type="int"
        , help="delay in seconds between looking for tasks to run")
    parser.add_option("--region", action="store", help="region this daemon runs in")
    parser.add_option("--threads", action="store_true"
        , help="use threading instead of subprocesses. \
        Note that threads in Python cannot be interrupted without killing the daemon!")
    parser.add_option("--no_log_redirect", action="store_true"
        , help="print daemon logging to sys.stdout & sys.stderr instead of redirecting them to a TMS log file.")
    parser.add_option("--debug", action="store_true", help="more messages")
    (options, args) = parser.parse_args()
    
    if options.debug:
        log.set_logging_debug(options.debug)
    
    if options.poll_frequency < 1:
        raise Exception("--poll_frequency must be >= 1")
    
    if not options.region:
        sys.exit(parser.get_usage())
    
    # resolve the region
    region = tms_models.ResourceRegion.get(options.region)
    if region == None:
        raise Exception("Don't know region '%s'" % (options.region))
    
    # register signal handlers for interrupt (ctl-c) & terminate ($ kill <pid>).
    def __handle_SIGINT__(signum, frame):
        assert signum == signal.SIGINT, "This signal handler only handles SIGINT, not '%s'. BUG!" % (signum)
        daemon.request_stop()
    def __handle_SIGTERM__(signum, frame):
        assert signum == signal.SIGTERM, "This signal handler only handles SIGTERM, not '%s'. BUG!" % (signum)
        daemon.request_kill()
    signal.signal(signal.SIGINT, __handle_SIGINT__)
    signal.signal(signal.SIGTERM, __handle_SIGTERM__)
    
    if options.threads:
        # multi-threaded; spawn new threads for new Tasks
        daemon = ThreadingNorcDaemon(region, options.poll_frequency, settings.TMS_LOG_DIR, not options.no_log_redirect)
    else:
        # single-threaded; fork new Tasks
        daemon = ForkingNorcDaemon(region, options.poll_frequency, settings.TMS_LOG_DIR, not options.no_log_redirect)
    
    ended_gracefully = daemon.run()
    if ended_gracefully:
        sys.exit(0)
    elif options.threads:
        # there's no way in python to interrupt threads; so gotta force 'em.
        # exit code is 137 on OS X
        os.kill(os.getpid(), signal.SIGKILL)
    else:
        sys.exit(137)

if __name__ == '__main__':
    main()
#
