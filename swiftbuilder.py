import os
import threading
import json
import gi

from gi.repository import GLib
from gi.repository import Gio
from gi.repository import GObject
from gi.repository import Ide

_ = Ide.gettext

_ATTRIBUTES = ",".join([
    Gio.FILE_ATTRIBUTE_STANDARD_NAME,
    Gio.FILE_ATTRIBUTE_STANDARD_DISPLAY_NAME,
    Gio.FILE_ATTRIBUTE_STANDARD_SYMBOLIC_ICON,
])

DEV_MODE = os.getenv('DEV_MODE') and True or False

class SwiftService(Ide.Object):
	_client = None
	_has_started = False
	_supervisor = None

	@classmethod
	def from_context(klass, context):
		return context.ensure_child_typed(SwiftService)

	@GObject.Property(type=Ide.LspClient)
	def client(self):
		return self._client

	@client.setter
	def client(self, value):
		self._client = value
		self.notify('client')

	def do_stop(self):
		if self._supervisor:
			supervisor, self._supervisor = self._supervisor, None
			supervisor.stop()



	def _ensure_started(self):
		# To avoid starting the process unconditionally at startup, lazily
		# start it when the first provider tries to bind a client to its
		# :client property.
		if not self._has_started:
			self._has_started = True
			
			launcher = self._create_launcher()
			launcher.set_clear_env(False)

			workdir = self.get_context().ref_workdir()
			launcher.set_cwd(workdir.get_path())

			# This will allows us to run commands in the host environment,
			launcher.push_argv('/bin/bash')
			launcher.push_argv('--login')
			launcher.push_argv('-c')

			launcher.push_argv('sourcekit-lsp')

			# Spawn our peer process and monitor it for
			# crashes. We may need to restart it occasionally.
			self._supervisor = Ide.SubprocessSupervisor()
			self._supervisor.connect('spawned', self._ls_spawned)
			self._supervisor.set_launcher(launcher)
			self._supervisor.start()

	def _ls_spawned(self, supervisor, subprocess):
		print("Spawned")
		stdin = subprocess.get_stdin_pipe()
		stdout = subprocess.get_stdout_pipe()
		io_stream = Gio.SimpleIOStream.new(stdout, stdin)

		if self._client:
			self._client.stop()
			self._client.destroy()

		self._client = Ide.LspClient.new(io_stream)
		self.append(self._client)
		self._client.add_language('swift')
		self._client.start()
		self.notify('client')
		print("Created new client")

	def _create_launcher(self):
		flags = Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE
		if not DEV_MODE:
			flags |= Gio.SubprocessFlags.STDERR_SILENCE
		launcher = Ide.SubprocessLauncher()
		launcher.set_flags(flags)
		launcher.set_cwd(GLib.get_home_dir())
		launcher.set_run_on_host(True)
		return launcher

	@classmethod
	def bind_client(klass, provider):
		context = provider.get_context()
		self = SwiftService.from_context(context)
		self._ensure_started()
		self.bind_property('client', provider, 'client', GObject.BindingFlags.SYNC_CREATE)

class SwiftCompletionProvider(Ide.LspCompletionProvider, Ide.CompletionProvider):
	def do_load(self, context):
		SwiftService.bind_client(self)

class SwiftHoverProvider(Ide.LspHoverProvider):
	def do_prepare(self):
		self.props.category = 'swift'
		self.props.priority = 100
		SwiftService.bind_client(self)

class SwiftSymbolResolver(Ide.LspSymbolResolver, Ide.SymbolResolver):
	def do_load(self):
		SwiftService.bind_client(self)

class SwiftDiagnosticResolver(Ide.LspDiagnosticProvider, Ide.DiagnosticProvider):
	def do_load(self):
		SwiftService.bind_client(self)

class SwiftBuildSystemDiscovery(Ide.SimpleBuildSystemDiscovery):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, ** kwargs)
		self.props.glob = 'Package.swift'
		self.props.hint = 'swiftbuilder'
		self.props.priority = 2000

class SwiftBuildService(Ide.Object, Ide.BuildSystem, Gio.AsyncInitable):
	project_file = GObject.Property(type = Gio.File)
	def do_get_id(self):
		return 'swift'
	
	def do_get_display_name(self):
		return 'Swift Build'

	def do_get_priority(self):
		return 2000

class SwiftPipelineAddin(Ide.Object, Ide.PipelineAddin):

	def do_load(self, pipeline):
		context = self.get_context()
		# Get the build system to use
		build_system = Ide.BuildSystem.from_context(context)
		
		# Check a swift builder has been generated
		if type(build_system) != SwiftBuildService:
			return

		config = pipeline.get_config()
		builddir = pipeline.get_builddir()
		runtime = config.get_runtime()
		srcdir = pipeline.get_srcdir()

		#if not runtime.contains_program_in_path('swift'):
		#	raise OSError('Swift not found in path')


		# Create a launcher to run 'swift build'
		build_launcher = pipeline.create_launcher()
		
		# run in the source directory
		build_launcher.set_cwd(srcdir)
		
		# we run the build command  using bash in order to run in the host environment, which is necessarily for incremental compilation
		build_launcher.push_argv('/bin/bash')
		build_launcher.push_argv('--login')
		build_launcher.push_argv('-c')
		
		build_launcher.push_argv("swift build")

		build_stage = Ide.PipelineStageLauncher.new(context, build_launcher)
		build_stage.set_name(_("Building Project"))
		build_stage.connect('query', self._query)
		self.track(pipeline.attach(Ide.PipelinePhase.BUILD, 0, build_stage))

	def _query(self, stage, pipeline, cancellable, extra):
		stage.set_completed(False)

class SwiftBuildTarget(Ide.Object, Ide.BuildTarget):
	
	def do_get_install_directory(self):
		return None

	def do_get_name(self):
		return "swift-run"

	def do_get_language(self):
		return 'swift'

	def do_get_cwd(self):
		context = self.get_context()
		project_file = Ide.BuildSystem.from_context(context).project_file
		return project_file.get_path()

	def do_get_argv(self):
		return ["/bin/bash", "--login", "-c", "swift run"]

class SwiftBuildTargetProvider(Ide.Object, Ide.BuildTargetProvider):
	
	def do_get_targets_async(self, cancellable, callback, data):
		print("Get targets async")
		task = Gio.Task.new(self, cancellable, callback)
		task.set_priority(GLib.PRIORITY_LOW)
		
		context = self.get_context()
		build_system = Ide.BuildSystem.from_context(context)

		if type(build_system) != SwiftBuildService:
			task.return_error(GLib.Error('Not a swift project', domain=GLib.quark_to_string(Gio.io_error_quark()), code=Gio.IOErrorEnum.NOT_SUPPORTED))
			return


		task.targets = [build_system.ensure_child_typed(SwiftBuildTarget)]
		task.return_boolean(True)

	def do_get_targets_finish(self, result):
		if result.propagate_boolean():
			return result.targets
