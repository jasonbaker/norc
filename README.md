
# Norc

Norc is a Task Management System that replaces Unix cron.  Its goal is to allow Tasks to be created, managed and audited in a flexible, user-friendly way.  Norc was first developed by [Darrell Silver](http://darrellsilver.com/) for use as the scheduling system for [Perpetually.com](http://www.perpetually.com/), the web archiving company.  It was open-sourced in October, 2009 at [NYC Python](http://www.nycpython.org/) at the suggestion of [David Christian](http://twitter.com/duganesque).

### Features

 * Define **Task dependencies**, ensuring that Task 'C' only runs after 'A' and 'B' have completed successfully.
 * Throttle limited resource with **resource management**, preventing processes from overloading a database, or other service.
 * Receive **email notification** when a Task fails. Full status is stored in the DB, allowing analysis of Task run times and success rates.
 * All output for Tasks is managed in **normalized logs**.
 * **Schedule** Tasks, just like Cron. 
 * Run Tasks on **any number of hosts**.  Task state is shared in a single DB, making Norc as scalable as its underlying database.
 * Set **timeouts** and **nice** values for any Task, catching errors and prevent runaway processing.
 * Because Task state is stored in a DB, it can be **administered through a web interface**.  These tools are currently limited to Django's administration tools and some custom command-line tools.
 * We built a plugin for **Amazon's SQS** that allows us to use all the auditing of Tasks, but using SQS as the source of Tasks to run.


### What Norc is Good at & What it's Not

* Norc is not SQS or RabbitMQ or Celery.  These systems are excellent at processing thousands or millions of the same Task, allowing background processing of repetitive actions.  Norc is better at handling arbitrary, diverse Tasks.  
* If you're encoding video that users upload, you're better off with a queueing system.  If you're currently using wrapper scripts, or want more transparency in complex, discreet processes, Norc can prove an excellent fit.
* At [Perpetually](http://www.perpetually.com/), we use Norc to kick off each scheduled crawl for each user, as well as several system administration Tasks.  When these Tasks run, all it does is put URLs into a queueing service for processing by any available worker.  Crawls can grow to several thousand URLs, and this process makes efficient use of Norc, which provides flexible editing and auditing, as well as queues, which offer excellent scalability and repeatability, but less run-time control.


### Architecture & Terminology Overview

Norc is written entirely in Python/Django.  It has been tested and rolled for Perpetually.com, running on OS X and Linux, using MySQL, Python 2.4, 2.5, 2.6 and Django-1.0.


#### Tasks:

A Task is a runnable unit of work. It is exactly analogous to a Unix 'cronjob'.  In Norc, Tasks are implemented by subclassing either the Task or ScheduleableTask interfaces.  There are several pre-built subclasses in Norc that cover common use cases:

 * **RunCommand**: allows running a single command.
 * **ScheduledRunCommand**: allows running a single command on a schedule

These classes are Django models, and as such each map to a table in the databse. All these base interfaces and subclasses are defined norc.core.models.py

Subclasses of Task & SchedulableTask must implement a few methods, and can safely override others:

 * **run()**: Mandatory: Action taken when this Task is run. Main processing happens here.
 * **get_library_name()**: Mandatory: The string path to this Task class name. This shouldn't be necessary, but it currently is.
 * **has_timeout()**: Mandatory boolean; True if this Task should timeout. False otherwise.
 * **get_timeout()**: Mandatory integer (seconds); return the number of seconds before this Task times out.  Must be defined if has_timeout() returns True.
 * **is_due_to_run()**: Boolean; defaults to True but can be overridden.  SchedulableTask has its own time-based implementation of this method.
 * **alert_on_failure()**: Boolean; defaults to True.

Most Tasks are designed to be run multiple times, such as on a daily our hourly basis. However, Norc provides flexibility on this:

 * **PERSISTENT**: Run each time it is due_to_run().  This applies to most tasks.
 * **EPHEMERAL**: Run once, and never again.  This is similar to an @ job, and is often paired with PERSISTENT Iterations (see below).

Task Statuses define the status of a single run of a single Task.  They are the the equivalent of exit statuses.  They are

 * **SKIPPED**: Task has been skipped; it ran and failed or did not run before being skipped
 * **RUNNING**: Task is running now.. OMG exciting!
 * **ERROR**: Task ran but ended in error
 * **TIMEDOUT**: Task timed out while running, and was killed by the Norc daemon that launched it.
 * **CONTINUE**: Task ran and failed. Child Tasks will still run allowed to run as though this Task succeeded.  This is the equivalent of if the dependency between this Task and its child was of type 'FLOW' (see details below).
 * **RETRY**: Task has been asked to be retried, but has yet to run again.
 * **SUCCESS**: Task ran successfully. Yay!


#### Jobs:

 * Each Task in Norc belongs to exactly 1 Job.  Dependencies between Tasks can only be defined within a single Job.
 * Jobs may be started on a schedule, such as midnight.  Norc uses a Job (TMS_ADMIN) to start all Jobs in Norc.


#### Iterations:

Each run of each Job does so as a distinct Iteration.  Iterations have three possible statuses: 

 * **RUNNING** (Tasks will be run as they become available)
 * **PAUSED** (The iteration has not completed but new Tasks will not be started)
 * **DONE** (No more tasks will be run for this job).

**Iteration Types** defines whether an Iteration is 

   * **EPHEMERAL**: The Iteration should run as long as Tasks in that Job for that iteration are incomplete.  Once all Tasks are complete (see Task Statuses below for details), the Iteration is marked as 'DONE'.  This is best used for a series of Tasks that run once a day, such as a data download Task followed by a data processes Task.  This is the most common type of Iteration.
   * **PERSISTENT**: The Iteration will remain running indefinitely, starting Tasks as they become available.  This is best used for Tasks that run once, such as EPHEMERAL Tasks.


#### Resources & Resource Relationships:

 * Norc allows usage of shared resources to be throttled, preventing too many Tasks from accessing a single resource, such as a web site or database with limited available connections.  Tasks can only be run in any Region that offers sufficient resources available at run time.
 * In addition to throttling limited resources, Resources are often used to target certain environments.  For example, if you have Tasks that can only run on Linux, then you should define a 'Linux' Resource, define a TaskResourceRelationship between this Task and 'Linux'.  This Task will then never run on any non-Linux host (see section on Daemons & Regions for how this works).
 * By default, each Task consumes 1 DATABASE_CONNECTION Resource.


#### Daemons:

 * Daemons in Norc (called TMSDaemons for no good reason) are responsible for kicking off all Tasks in Norc.  A daemon is a unix process running on a specific host running as tmsd.py.


#### Regions:

 * Regions are islands of Resource availability.  Each Daemon runs within a single Region.


#### Dependency Types:

Tasks in the same Job can define Dependencies that create a parent -> child relationship between Tasks.  Child Tasks will only run once all their Parent's have satisfactorily completed.

Typically a child Task only runs once its parents have completed successfully, but this can be altered using Dependency Types:
 * **DEP_TYPE_STRICT**: Child Tasks only run when the parent has completed successfully.  This is the most common type of dependency.
 * **DEP_TYPE_FLOW**: Child Tasks run as soon as the parent has completed, regardless of the parent's exit status.


### Interacting & Monitoring:

Norc could support a web front end that allows full administration of the entire system, but none currently exists. Instead, Norc makes use of Django's excellent Admin interface.

#### tmsdctl.py:

 * Allows stopping, killing, viewing of all Daemons in Norc.  It also allows an overview of Tasks run by each Daemon.  
 * This sample from [perpetually.com](http://www.perpetually.com/) shows two daemons running on two distinct hosts in two distinct regions:

        $ tmsdctl 
        Status as of 10/27/2009 19:47:27
        6 INTERESTING tms daemon(s):
        ID     Type     Region    Host          PID     Running   Success   Error    Status               Started   Ended
        409     TMS     perp1     perpetually   14031         6       120       3   RUNNING   2009-10-24 18:52:52       -
        413     TMS     perp3     perp3         15159         2      2283       0   RUNNING   2009-10-24 19:01:26       -

 * This sample from [perpetually.com](http://www.perpetually.com/) shows a snippet of details for daemon ID 409.  We see the status of just four Tasks in this daemon:

        $ tmsdctl --det 410
        Status as of 10/27/2009 19:50:00
        1 INTERESTING tms daemon(s):
        ID     Type     Region    Host          PID     Running   Success   Error    Status               Started   Ended
        409     TMS     perp1     perpetually   14031         6       120       3   RUNNING   2009-10-24 18:52:52       -
        
        TMS Daemon perp1:410 (RUNNING) manages 3 task(s):
        
        Task ID      Status               Started                 Ended
        7546134    TIMEDOUT   2009-10-25 00:05:30   2009-10-25 00:20:30
        7546188       ERROR   2009-10-25 00:08:55   2009-10-25 00:10:05
        7546048       ERROR   2009-10-25 00:09:48   2009-10-25 00:09:48
        7546205     RUNNING   2009-10-27 00:18:54   2009-10-25 00:18:55
        ...


### Code Base & Development Status:

Norc is stable, but there are known issues & limitations:

 * Log files are currently stored only on the host on which the Task ran.  This limits their accessibility, and could be remedied through pushing them to S3, or other central service. They're just text files.
 * Processes instead of threading Tasks
 * No configurable environments

Norc was first developed by [Darrell Silver](http://darrellsilver.com/) as the archiving scheduling system for [Perpetually.com's](http://www.perpetually.com/) archiving system, and is currently in production.   [Perpetually.com](http://www.perpetually.com/) lets you capture and archive any web site with a single click. It's the history of the internet made useful.  A core feature of [Perpetually's](http://www.perpetually.com/) offering is repeated, scheduled archives, a Task for which Norc has proven a good fit.


### Install & Example:

See ./INSTALL.md


