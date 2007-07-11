#!/usr/bin/python
#
# Copyright 2007 Google Inc. Released under the GPL v2

"""This module defines the Autotest class

	Autotest: software to run tests automatically
"""

__author__ = """mbligh@google.com (Martin J. Bligh),
poirier@google.com (Benjamin Poirier),
stutsman@google.com (Ryan Stutsman)"""


import re
import os
import sys
import subprocess
import urllib
import tempfile
import shutil

import installable_object
import errors
import utils


AUTOTEST_SVN  = 'svn://test.kernel.org/autotest/trunk/client'
AUTOTEST_HTTP = 'http://test.kernel.org/svn/autotest/trunk/client'

# Timeouts for powering down and up respectively
HALT_TIME = 300
BOOT_TIME = 300


class AutotestRunError(errors.AutoservRunError):
	pass


class Autotest(installable_object.InstallableObject):
	"""This class represents the Autotest program.

	Autotest is used to run tests automatically and collect the results.
	It also supports profilers.

	Implementation details:
	This is a leaf class in an abstract class hierarchy, it must
	implement the unimplemented methods in parent classes.
	"""
	def __init__(self):
		super(Autotest, self).__init__()
	
	def get_from_file(self, filename):
		"""Specify a tarball on the local filesystem from which
		autotest will be installed.
		
		Args:
			filename: a str specifying the path to the tarball
		
		Raises:
			AutoservError: if filename does not exist
		"""
		if os.path.exists(filename):
			self.__filename = filename
		else:
			raise errors.AutoservError('%s not found' % filename)
	
	def get_from_url(self, url):
		"""Specify a url to a tarball from which autotest will be
		installed.
		
		Args:
			url: a str specifying the url to the tarball
		"""
		self.__filename = utils.get(url)
	
	def install(self, host):
		"""Install autotest from the specified tarball (either
		via get_from_file or get_from_url).  If neither were
		called an attempt will be made to install from the
		autotest svn repository.
		
		Args:
			host: a Host instance on which autotest will be
				installed
		
		Raises:
			AutoservError: if a tarball was not specified and
				the target host does not have svn installed in its path
		"""
		# try to install from file or directory
		try:
			if os.path.isdir(self.__filename):
				# Copy autotest recursively
				autodir = _get_autodir(host)
				host.run('mkdir -p %s' %
					 utils.scp_remote_escape(autodir))
				host.send_file(self.__filename + '/',
					       autodir)
			else:
				# Copy autotest via tarball
				raise "Not yet implemented!"
			return
		except AttributeError, e:
			pass

		# if that fails try to install using svn
		if utils.run('which svn').exit_status:
			raise AutoservError('svn not found in path on \
			target machine: %s' % host.name)
		try:
			host.run('svn checkout %s %s' %
				 (AUTOTEST_SVN, _get_autodir(host)))
		except errors.AutoservRunError, e:
			host.run('svn checkout %s %s' %
				 (AUTOTEST_HTTP, _get_autodir(host)))


	def run(self, control_file, results_dir, host):
		"""
		Run an autotest job on the remote machine.

		Args:
			control_file: an open file-like-obj of the control file
			results_dir: a str path where the results should be stored
				on the local filesystem
			host: a Host instance on which the control file should
				be run
		
		Raises:
			AutotestRunError: if there is a problem executing
				the control file
		"""
		atrun = _Run(host, results_dir)
		atrun.verify_machine()
		debug = os.path.join(results_dir, 'debug')
		if not os.path.exists(debug):
			os.makedirs(debug)

		# Ready .... Aim ....
		host.run('rm -f ' + atrun.remote_control_file)
		host.run('rm -f ' + atrun.remote_control_file + '.state')

		# Copy control_file to remote_control_file on the host
		tmppath = utils.get(control_file)
		host.send_file(tmppath, atrun.remote_control_file)
		os.remove(tmppath)

		atrun.execute_control()

		# retrive results
		results = os.path.join(atrun.autodir, 'results', 'default')
		# Copy all dirs in default to results_dir
		host.get_file(results + '/', results_dir)


class _Run(object):
	"""
	Represents a run of autotest control file.  This class maintains
	all the state necessary as an autotest control file is executed.

	It is not intended to be used directly, rather control files
	should be run using the run method in Autotest.
	"""
	def __init__(self, host, results_dir):
		self.host = host
		self.results_dir = results_dir

		self.autodir = _get_autodir(self.host)
		self.remote_control_file = os.path.join(self.autodir, 'control')


	def verify_machine(self):
		binary = os.path.join(self.autodir, 'bin/autotest')
		self.host.run('ls ' + binary)


	def __execute_section(self, section):
		print "Executing %s/bin/autotest %s/control phase %d" % \
					(self.autodir, self.autodir,
					 section)
		logfile = "%s/debug/client.log.%d" % (self.results_dir,
						      section)
		client_log = open(logfile, 'w')
		if section > 0:
			cont = '-c'
		else:
			cont = ''
		client = os.path.join(self.autodir, 'bin/autotest_client')
		ssh = "ssh -q %s@%s" % (self.host.user, self.host.hostname)
		cmd = "%s %s %s" % (client, cont, self.remote_control_file)
		print "%s '%s'" % (ssh, cmd)
		# Use Popen here, not m.ssh, as we want it in the background
		p = subprocess.Popen("%s '%s'" % (ssh, cmd), shell=True, \
				stdout=client_log, stderr=subprocess.PIPE)
		line = None
		for line in iter(p.stderr.readline, ''):
			print line,
			sys.stdout.flush()
		if not line:
			raise AutotestRunError("execute_section: %s '%s' \
			failed to return anything" % (ssh, cmd))
		return line


	def execute_control(self):
		section = 0
		while True:
			last = self.__execute_section(section)
			section += 1
			if re.match('DONE', last):
				print "Client complete"
				return
			elif re.match('REBOOT', last):
				print "Client is rebooting"
				print "Waiting for client to halt"
				if not self.host.wait_down(HALT_TIME):
					raise AutotestRunError("%s \
					failed to shutdown after %ds" %
						       (self.host.hostname,
							HALT_TIME))
				print "Client down, waiting for restart"
				if not self.host.wait_up(BOOT_TIME):
					# since reboot failed
					# hardreset the machine once if possible
					# before failing this control file
					if hasattr(self.host, 'hardreset'):
						print "Hardresetting %s" % self.hostname
						self.host.hardreset()
					raise AutotestRunError("%s \
					failed to boot after %ds" %
						       (self.host.hostname,
							BOOT_TIME))
				continue
			raise AutotestRunError("Aborting - unknown \
			return code: %s\n" % last)


def _get_autodir(host):
	try:
		atdir = host.run(
			'grep autodir= /etc/autotest.conf').stdout.strip(" \n")
		if atdir:
			m = re.search(r'autodir *= *[\'"]?([^\'"]*)[\'"]?',
				      atdir)
			return m.group(1)
	except errors.AutoservRunError:
		pass
	for path in ['/usr/local/autotest', '/home/autotest']:
		try:
			host.run('ls ' + path)
			return path
		except errors.AutoservRunError:
			pass
	raise AutotestRunError("Cannot figure out autotest directory")
