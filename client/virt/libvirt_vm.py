"""
Utility classes and functions to handle Virtual Machine creation using qemu.

@copyright: 2008-2009 Red Hat Inc.
"""

import time, os, logging, fcntl, re, commands
from autotest_lib.client.common_lib import error
from autotest_lib.client.bin import utils
from xml.dom import minidom
import virt_utils, virt_vm, aexpect
import libvirt


def libvirtd_restart():
    """
    Restart libvirt daemon.
    """
    logging.debug("libvirtd Restart")
    status, domstate = commands.getstatusoutput("service libvirtd restart")
    logging.debug("status=%d, domstate=%s", status, domstate.strip())
    if status == 0:
        logging.debug("Restarted libvirtd successfuly")
        logging.debug("Allowing things to settle after libvirtd restart. "
                      "Sleeping 30s.")
        time.sleep(30)
        return True
    else:
        logging.debug("Failed to Restart libvirtd")
        return False


class VM(virt_vm.BaseVM):
    """
    This class handles all basic VM operations for libvirt.
    """
    def __init__(self, name, params, root_dir, address_cache, state=None):
        """
        Initialize the object and set a few attributes.

        @param name: The name of the object
        @param params: A dict containing VM params
                (see method make_virt_install_command for a full description)
        @param root_dir: Base directory for relative filenames
        @param address_cache: A dict that maps MAC addresses to IP addresses
        @param state: If provided, use this as self.__dict__
        """
        virt_vm.BaseVM.__init__(self, name, params)

        if state:
            self.__dict__ = state
        else:
            self.process = None
            self.serial_console = None
            self.redirs = {}
            self.vnc_port = 5900
            self.pci_assignable = None
            self.netdev_id = []
            self.device_id = []
            self.pci_devices = []
            self.uuid = None

        self.spice_port = 8000
        self.name = name
        self.params = params
        self.root_dir = root_dir
        self.address_cache = address_cache

        self.connect = None
        self.dom = None


    def verify_alive(self):
        """
        Make sure the VM is alive.

        @raise VMDeadError: If the VM is dead
        """
        try:
            self.is_alive()
        except virt_vm.VMDeadError:
            raise virt_vm.VMDeadError(self.process.get_status(),
                                      self.process.get_output())


    def is_alive(self):
        """
        Return True if VM is alive.
        """
        if self.domain is None:
            return False
        
        if self.domain.isActive():
            return True
        else:
            return Fasle


    def is_dead(self):
        """
        Return True if VM is dead.
        """
        return not self.is_alive()


    def clone(self, name=None, params=None, root_dir=None, address_cache=None,
              copy_state=False):
        """
        Return a clone of the VM object with optionally modified parameters.
        The clone is initially not alive and needs to be started using create().
        Any parameters not passed to this function are copied from the source
        VM.

        @param name: Optional new VM name
        @param params: Optional new VM creation parameters
        @param root_dir: Optional new base directory for relative filenames
        @param address_cache: A dict that maps MAC addresses to IP addresses
        @param copy_state: If True, copy the original VM's state to the clone.
                Mainly useful for make_virt_install_command().
        """
        if name is None:
            name = self.name
        if params is None:
            params = self.params.copy()
        if root_dir is None:
            root_dir = self.root_dir
        if address_cache is None:
            address_cache = self.address_cache
        if copy_state:
            state = self.__dict__.copy()
        else:
            state = None
        return VM(name, params, root_dir, address_cache, state)


    def __make_libvirt_command(self, name=None, params=None, root_dir=None):
        """
        Generate a libvirt command line. All parameters are optional. If a
        parameter is not supplied, the corresponding value stored in the
        class attributes is used.

        @param name: The name of the object
        @param params: A dict containing VM params
        @param root_dir: Base directory for relative filenames

        @note: The params dict should contain:
               mem -- memory size in MBs
               cdrom -- ISO filename to use with the qemu -cdrom parameter
               extra_params -- a string to append to the qemu command
               shell_port -- port of the remote shell daemon on the guest
               (SSH, Telnet or the home-made Remote Shell Server)
               shell_client -- client program to use for connecting to the
               remote shell daemon on the guest (ssh, telnet or nc)
               x11_display -- if specified, the DISPLAY environment variable
               will be be set to this value for the qemu process (useful for
               SDL rendering)
               images -- a list of image object names, separated by spaces
               nics -- a list of NIC object names, separated by spaces

               For each image in images:
               drive_format -- string to pass as 'if' parameter for this
               image (e.g. ide, scsi)
               image_snapshot -- if yes, pass 'snapshot=on' to qemu for
               this image
               image_boot -- if yes, pass 'boot=on' to qemu for this image
               In addition, all parameters required by get_image_filename.

               For each NIC in nics:
               nic_model -- string to pass as 'model' parameter for this
               NIC (e.g. e1000)
        """
        # helper function for command line option wrappers
        def has_option(help, option):
            return bool(re.search(r"--%s" % option, help, re.MULTILINE))

        # Wrappers for all supported libvirt command line parameters.
        # This is meant to allow support for multiple libvirt versions.
        # Each of these functions receives the output of 'libvirt --help' as a
        # parameter, and should add the requested command line option
        # accordingly.

        def add_name(help, name):
            return " --name '%s'" % name

        def add_hvm_or_pv(help, hvm_or_pv):
            return " %s" % hvm_or_pv

        def add_mem(help, mem):
            return " --ram=%s" % mem

        def add_check_cpu(help):
            if has_option(help, "check-cpu"):
                return " --check-cpu"
            else:
                return ""

        def add_smp(help, smp):
            return " --vcpu=%s" % smp

        def add_location(help, location):
            #return " --location %s" % location
            if has_option(help, "location"):
                return " --location %s" % location
            else:
                return ""

        def add_cdrom(help, filename, index=None):
            if has_option(help, "cdrom"):
                return " --cdrom %s" % filename
            else:
                return ""

        def add_pxe(help):
            if has_option(help, "pxe"):
                return " --pxe"
            else:
                return ""

        def add_drive(help, filename, pool=None, vol=None, device=None,
                      bus=None, perms=None, size=None, sparse=False,
                      cache=None, format=None):
            cmd = " --disk"
            if filename:
                cmd += " path=%s" % filename
            elif pool:
                if vol:
                    cmd += " vol=%s/%s" % (pool, vol)
                else:
                    cmd += " pool=%s" % pool
            if device:
                cmd += ",device=%s" % device
            if bus:
                cmd += ",bus=%s" % bus
            if perms:
                cmd += ",%s" % perms
            if size:
                cmd += ",size=%s" % size.rstrip("Gg")
            if sparse:
                cmd += ",sparse=false"
            if format:
                cmd += ",format=%s" % format
            return cmd

        def add_floppy(help, filename):
            return " --disk path=%s,device=floppy,ro" % filename

        def add_vnc(help, vnc_port):
            return " --vnc --vncport=%d" % (vnc_port)

        def add_sdl(help):
            if has_option(help, "sdl"):
                return " --sdl"
            else:
                return ""

        def add_nographic(help):
            return " --nographics"

        def add_video(help, video_device):
            if has_option(help, "video"):
                return " --video=%s" % (video_device)
            else:
                return ""

        def add_uuid(help, uuid):
            if has_option(help, "uuid"):
                return " --uuid %s" % uuid
            else:
                return ""

        def add_os_type(help, os_type):
            if has_option(help, "os-type"):
                return " --os-type %s" % os_type
            else:
                return ""

        def add_os_variant(help, os_variant):
            if has_option(help, "os-variant"):
                return " --os-variant %s" % os_variant
            else:
                return ""

        def add_pcidevice(help, pci_device):
            if has_option(help, "host-device"):
                return " --host-device %s" % pci_device
            else:
                return ""

        def add_soundhw(help, sound_device):
            if has_option(help, "soundhw"):
                return " --soundhw %s" % sound_device
            else:
                return ""

        def add_serial(help, filename):
            if has_option(help, "serial"):
                return "  --serial file,path=%s --serial pty" % filename
            else:
                return ""

        def add_kernel_cmdline(help, cmdline):
            return " -append %s" % cmdline

        # End of command line option wrappers

        if name is None:
            name = self.name
        if params is None:
            params = self.params
        if root_dir is None:
            root_dir = self.root_dir

        # Clone this VM using the new params
        vm = self.clone(name, params, root_dir, copy_state=True)

        virt_install_binary = virt_utils.get_path(root_dir,
                                    params.get("virt_install_binary",
                                    "virt-install"))

        help = commands.getoutput("%s --help" % virt_install_binary)

        # Start constructing the qemu command
        virt_install_cmd = ""
        # Set the X11 display parameter if requested
        if params.get("x11_display"):
            virt_install_cmd += "DISPLAY=%s " % params.get("x11_display")
        # Add the qemu binary
        virt_install_cmd += virt_install_binary

        # hvm or pv specificed by libvirt switch (pv used  by Xen only)
        hvm_or_pv = params.get("hvm_or_pv")
        if hvm_or_pv:
            virt_install_cmd += add_hvm_or_pv(help, hvm_or_pv)

        # Add the VM's name
        virt_install_cmd += add_name(help, name)

        mem = params.get("mem")
        if mem:
            virt_install_cmd += add_mem(help, mem)

        # TODO: should we do the check before we call ? negative case ?
        check_cpu = params.get("use_check_cpu")
        if check_cpu:
            virt_install_cmd += add_check_cpu(help)

        smp = params.get("smp")
        if smp:
            virt_install_cmd += add_smp(help, smp)

        # TODO: directory location for vmlinuz/kernel for cdrom install ?
        location = None
        if params.get("medium") == 'url':
            location = params.get("url")
        elif params.get("medium") == 'kernel_initrd':
            # directory location of kernel/initrd pair (directory layout must
            # be in format libvirt will recognize)
            location = params.get("image_dir")
        elif params.get("medium") == 'nfs':
            location = "nfs:%s:%s" % (params.get("nfs_server"),
                                      params.get("nfs_dir"))
        elif params.get("medium") == 'cdrom':
            if params.get("use_libvirt_cdrom_switch") == 'yes':
                virt_install_cmd += add_cdrom(help, params.get("cdrom_cd1"))
            else:
                location = params.get("image_dir")

        if location:
            virt_install_cmd += add_location(help, location)

        if params.get("display") == "vnc":
            if params.get("vnc_port"):
                vm.vnc_port = int(params.get("vnc_port"))
            virt_install_cmd += add_vnc(help, vm.vnc_port)
        elif params.get("display") == "sdl":
            virt_install_cmd += add_sdl(help)
        elif params.get("display") == "nographic":
            virt_install_cmd += add_nographic(help)

        video_device = params.get("video_device")
        if video_device:
            virt_install_cmd += add_video(help, video_device)

        sound_device = params.get("sound_device")
        if sound_device:
            virt_install_cmd += add_soundhw(help, sound_device)

        # if none is given a random UUID will be generated by libvirt
        if params.get("uuid"):
            virt_install_cmd += add_uuid(help, params.get("uuid"))

        # selectable OS type
        if params.get("use_os_type") == "yes":
            virt_install_cmd += add_os_type(help, params.get("os_type"))

        # selectable OS variant
        if params.get("use_os_variant") == "yes":
            virt_install_cmd += add_os_variant(help, params.get("os_variant"))

        # If the PCI assignment step went OK, add each one of the PCI assigned
        # devices to the command line.
        if self.pci_devices:
            for pci_id in self.pci_devices:
                virt_install_cmd += add_pcidevice(help, pci_id)

        for image_name in params.objects("images"):
            image_params = params.object_params(image_name)
            filename = virt_vm.get_image_filename(image_params, root_dir)
            if image_params.get("use_storage_pool") == "yes":
                filename = None
            if image_params.get("boot_drive") == "no":
                continue
            virt_install_cmd += add_drive(help,
                             filename,
                                  image_params.get("image_pool"),
                                  image_params.get("image_vol"),
                                  image_params.get("image_device"),
                                  image_params.get("image_bus"),
                                  image_params.get("image_perms"),
                                  image_params.get("image_size"),
                                  image_params.get("drive_sparse"),
                                  image_params.get("drive_cache"),
                                  image_params.get("image_format"))

        for cdrom in params.objects("cdroms"):
            cdrom_params = params.object_params(cdrom)
            iso = cdrom_params.get("cdrom")
            if params.get("use_libvirt_cdrom_switch") == 'yes':
                # we don't want to skip the winutils iso
                if not cdrom == 'winutils':
                    logging.debug("Using --cdrom instead of --disk for install")
                    logging.debug("Skipping CDROM:%s:%s", cdrom, iso)
                    continue
            if params.get("medium") == 'cdrom_no_kernel_initrd':
                if iso == params.get("cdrom_cd1"):
                    logging.debug("Using cdrom or url for install")
                    logging.debug("Skipping CDROM: %s", iso)
                    continue

            if iso:
                virt_install_cmd += add_drive(help,
                             virt_utils.get_path(root_dir, iso),
                                  image_params.get("iso_image_pool"),
                                  image_params.get("iso_image_vol"),
                                  'cdrom',
                                  None,
                                  None,
                                  None,
                                  None,
                                  None,
                                  None)

        # We may want to add {floppy_otps} parameter for -fda
        # {fat:floppy:}/path/. However vvfat is not usually recommended.
        floppy = params.get("floppy")
        if floppy:
            floppy = virt_utils.get_path(root_dir, floppy)
            virt_install_cmd += add_drive(help, floppy,
                              None,
                              None,
                              'floppy',
                              None,
                              None,
                              None,
                              None,
                              None,
                              None)

        # FIXME: for now in the pilot always add mac address to virt-install
        vlan = 0
        mac = vm.get_mac_address(vlan)
        if mac:
            virt_install_cmd += " --mac %s" % mac
            self.nic_mac = mac

        virt_install_cmd += (" --network %s,model=%s" %
                             (params.get("virsh_network"),
                              params.get("nic_model")))

        if params.get("use_no_reboot") == "yes":
            virt_install_cmd += " --noreboot"

        if params.get("use_autostart") == "yes":
            virt_install_cmd += " --autostart"

        if params.get("virt_install_debug") == "yes":
            virt_install_cmd += " --debug"

        # bz still open, not fully functional yet
        if params.get("use_virt_install_wait") == "yes":
            virt_install_cmd += (" --wait %s" %
                                 params.get("virt_install_wait_time"))

        extra_params = params.get("extra_params")
        if extra_params:
            virt_install_cmd += " --extra-args '%s'" % extra_params

        virt_install_cmd += " --noautoconsole"

        return virt_install_cmd


    @error.context_aware
    def create(self, name=None, params=None, root_dir=None, timeout=5.0,
               migration_mode=None, mac_source=None):
        """
        Start the VM by running a virt_install command.
        All parameters are optional. If name, params or root_dir are not
        supplied, the respective values stored as class attributes are used.

        @param name: The name of the object
        @param params: A dict containing VM params
        @param root_dir: Base directory for relative filenames
        @param migration_mode: If supplied, start VM for incoming migration
                using this protocol (either 'tcp', 'unix' or 'exec')
        @param migration_exec_cmd: Command to embed in '-incoming "exec: ..."'
                (e.g. 'gzip -c -d filename') if migration_mode is 'exec'
        @param mac_source: A VM object from which to copy MAC addresses. If not
                specified, new addresses will be generated.

        @raise VMCreateError: If virt_install terminates unexpectedly
        @raise VMHugePageError: If hugepage initialization fails
        @raise VMImageMissingError: If a CD image is missing
        @raise VMHashMismatchError: If a CD image hash has doesn't match the
                expected hash
        @raise VMBadPATypeError: If an unsupported PCI assignment type is
                requested
        @raise VMPAError: If no PCI assignable devices could be assigned
        """
        error.context("creating '%s'" % self.name)
        self.destroy(free_mac_addresses=False)

        if name is not None:
            self.name = name
        if params is not None:
            self.params = params
        if root_dir is not None:
            self.root_dir = root_dir
        name = self.name
        params = self.params
        root_dir = self.root_dir

        # Verify the md5sum of the ISO images
        for cdrom in params.objects("cdromss"):
            cdrom_params = params.object_params(cdrom)
            iso = cdrom_params.get("cdrom")
            if iso:
                iso = virt_utils.get_path(root_dir, iso)
                if not os.path.exists(iso):
                    raise virt_vm.VMImageMissingError(iso)
                compare = False
                if cdrom_params.get("md5sum_1m"):
                    logging.debug("Comparing expected MD5 sum with MD5 sum of "
                                  "first MB of ISO file...")
                    actual_hash = utils.hash_file(iso, 1048576, method="md5")
                    expected_hash = cdrom_params.get("md5sum_1m")
                    compare = True
                elif cdrom_params.get("md5sum"):
                    logging.debug("Comparing expected MD5 sum with MD5 sum of "
                                  "ISO file...")
                    actual_hash = utils.hash_file(iso, method="md5")
                    expected_hash = cdrom_params.get("md5sum")
                    compare = True
                elif cdrom_params.get("sha1sum"):
                    logging.debug("Comparing expected SHA1 sum with SHA1 sum "
                                  "of ISO file...")
                    actual_hash = utils.hash_file(iso, method="sha1")
                    expected_hash = cdrom_params.get("sha1sum")
                    compare = True
                if compare:
                    if actual_hash == expected_hash:
                        logging.debug("Hashes match")
                    else:
                        raise virt_vm.VMHashMismatchError(actual_hash,
                                                          expected_hash)

        # Make sure the following code is not executed by more than one thread
        # at the same time
        lockfile = open("/tmp/libvirt-autotest-vm-create.lock", "w+")
        fcntl.lockf(lockfile, fcntl.LOCK_EX)

        try:
            # Handle port redirections
            redir_names = params.objects("redirs")
            host_ports = virt_utils.find_free_ports(5000, 6000, len(redir_names))
            self.redirs = {}
            for i in range(len(redir_names)):
                redir_params = params.object_params(redir_names[i])
                guest_port = int(redir_params.get("guest_port"))
                self.redirs[guest_port] = host_ports[i]

            # Generate netdev/device IDs for all NICs
            self.netdev_id = []
            self.device_id = []
            for nic in params.objects("nics"):
                self.netdev_id.append(virt_utils.generate_random_id())
                self.device_id.append(virt_utils.generate_random_id())

            # Find available PCI devices
            self.pci_devices = []
            for device in params.objects("pci_devices"):
                self.pci_devices.append(device)

            # Find available VNC port, if needed
            if params.get("display") == "vnc":
                self.vnc_port = virt_utils.find_free_port(5900, 6100)

            # Find available spice port, if needed
            if params.get("spice"):
                self.spice_port = virt_utils.find_free_port(8000, 8100)

            # Find random UUID if specified 'uuid = random' in config file
            if params.get("uuid") == "random":
                f = open("/proc/sys/kernel/random/uuid")
                self.uuid = f.read().strip()
                f.close()

            # Generate or copy MAC addresses for all NICs
            num_nics = len(params.objects("nics"))
            for vlan in range(num_nics):
                nic_name = params.objects("nics")[vlan]
                nic_params = params.object_params(nic_name)
                mac = (nic_params.get("nic_mac") or
                       mac_source and mac_source.get_mac_address(vlan))
                if mac:
                    virt_utils.set_mac_address(self.instance, vlan, mac)
                else:
                    virt_utils.generate_mac_address(self.instance, vlan)

            # Make qemu command
            virt_install_command = self.__make_libvirt_command()

            # Add migration parameters if required
            if migration_mode == "tcp":
                self.migration_port = virt_utils.find_free_port(5200, 6000)
                virt_install_command += " -incoming tcp:0:%d" % self.migration_port
            elif migration_mode == "unix":
                self.migration_file = "/tmp/migration-unix-%s" % self.instance
                virt_install_command += " -incoming unix:%s" % self.migration_file
            elif migration_mode == "exec":
                self.migration_port = virt_utils.find_free_port(5200, 6000)
                virt_install_command += (' -incoming "exec:nc -l %s"' %
                                 self.migration_port)

            logging.info("Running virt_install command:\n%s", virt_install_command)
            self.process = aexpect.run_bg(virt_install_command, None, logging.info,
                                          "(libvirt) ")

            # Time needed for the domain to be created
            time.sleep(5)

            # Establish a session with the serial console -- requires a version
            # of netcat that supports -U
            self.serial_console = aexpect.ShellSession(
                "nc -U %s" % self.get_serial_console_filename(),
                auto_close=False,
                output_func=virt_utils.log_line,
                output_params=("serial-%s.log" % name,))

        finally:
            fcntl.lockf(lockfile, fcntl.LOCK_UN)
            lockfile.close()


    def destroy(self, gracefully=True, free_mac_addresses=True):
        """
        Destroy the VM.

        If gracefully is True, first attempt to shutdown the VM with a shell
        command. If that fails, send SIGKILL to the qemu process.

        @param gracefully: If True, an attempt will be made to end the VM
                using a shell command before trying to end the qemu process
                with a 'quit' or a kill signal.
        @param free_mac_addresses: If True, the MAC addresses used by the VM
                will be freed.
        """
        # Is it already dead?
        if self.is_dead():
            return

        logging.debug("Destroying VM")
        if gracefully == True:
            self.dom.shutdown()
          
        if virt_utils.wait_for(self.is_dead, 60, 1, 1):
            logging.debug("VM is down")
            return
  
        self.dom.destroy()

        if virt_utils.wait_for(self.is_dead, 5, 0.5, 0.5):
            logging.debug("VM is down")
            return

        logging.error("Can't destroy domain")


    def get_address(self, index=0):
        """
        Return the address of a NIC of the guest, in host space.

        If port redirection is used, return 'localhost' (the NIC has no IP
        address of its own).  Otherwise return the NIC's IP address.

        @param index: Index of the NIC whose address is requested.
        @raise VMMACAddressMissingError: If no MAC address is defined for the
                requested NIC
        @raise VMIPAddressMissingError: If no IP address is found for the the
                NIC's MAC address
        @raise VMAddressVerificationError: If the MAC-IP address mapping cannot
                be verified (using arping)
        """
        nics = self.params.objects("nics")
        nic_name = nics[index]
        nic_params = self.params.object_params(nic_name)
        if nic_params.get("nic_mode") == "tap":
            mac = self.get_mac_address(index).lower()
            # Get the IP address from the cache
            ip = self.address_cache.get(mac)
            if not ip:
                raise virt_vm.VMIPAddressMissingError(mac)
            # Make sure the IP address is assigned to this guest
            macs = [self.get_mac_address(i) for i in range(len(nics))]
            if not virt_utils.verify_ip_address_ownership(ip, macs):
                raise virt_vm.VMAddressVerificationError(mac, ip)
            return ip
        else:
            return "localhost"    


    def get_mac_address(self, nic_index=0):
        """
        Return the MAC address of a NIC.

        @param nic_index: Index of the NIC
        @raise VMMACAddressMissingError: If no MAC address is defined for the
                requested NIC
        """
        nic_name = self.params.objects("nics")[nic_index]
        nic_params = self.params.object_params(nic_name)
        if self.params.objects("type")[0] == 'unattended_install':
            mac = virt_utils.get_mac_address(self.instance, nic_index)
        else:
            thexml = self.domain.XMLDesc(libvirt.VIR_DOMAIN_XML_UPDATE_CPU)
            dom = minidom.parseString(thexml)
            count = 0
            for node in dom.getElementsByTagName('interface'):
                source = node.childNodes[1]
                x = source.attributes["address"]
                if nic_index == count:
                    mac = x.value
                    return 
                count += 1

        if not mac:
            raise virt_vm.VMMACAddressMissingError(nic_index)
        return mac


    def free_mac_address(self, nic_index=0):
        """
        Free a NIC's MAC address.

        @param nic_index: Index of the NIC
        """
        virt_utils.free_mac_address(self.instance, nic_index)


    def get_pid(self):
        """
        Return the VM's PID.  If the VM is dead return None.

        @note: This works under the assumption that self.process.get_pid()
        returns the PID of the parent shell process.
        """
        try:
            filename = "/var/run/libvirt/qemu/%s.pid" % self.name
            if not self.params.get("type") == "unattended_install":
                if os.path.exists(filename):
                    self.process = int(open(filename).read())
            children = commands.getoutput("ps --ppid=%d -o pid=" %
                                          self.process.get_pid()).split()
            return int(children[0])
        except (TypeError, IndexError, ValueError):
            return None


    def get_shell_pid(self):
        """
        Return the PID of the parent shell process.

        @note: This works under the assumption that self.process.get_pid()
        returns the PID of the parent shell process.
        """
        return self.process.get_pid()


    def get_shared_meminfo(self):
        """
        Returns the VM's shared memory information.

        @return: Shared memory used by VM (MB)
        """
        if self.is_dead():
            logging.error("Could not get shared memory info from dead VM.")
            return None

        filename = "/proc/%d/statm" % self.get_pid()
        shm = int(open(filename).read().split()[2])
        # statm stores informations in pages, translate it to MB
        return shm * 4.0 / 1024


    @error.context_aware
    def reboot(self, session=None, method="shell", nic_index=0, timeout=240):
        """
        Reboot the VM and wait for it to come back up by trying to log in until
        timeout expires.

        @param session: A shell session object or None.
        @param method: Reboot method.  Can be "shell" (send a shell reboot
                command) or "libvirt" (call libvirt function).
        @param nic_index: Index of NIC to access in the VM, when logging in
                after rebooting.
        @param timeout: Time to wait for login to succeed (after rebooting).
        @return: A new shell session object.
        """
        error.base_context("rebooting '%s'" % self.name, logging.info)
        error.context("before reboot")
        session = session or self.login()
        error.context()

        if method == "shell":
            session.sendline(self.params.get("reboot_command"))
        else:
            self.domain.reboot()

        error.context("waiting for guest to go down", logging.info)
        if not virt_utils.wait_for(lambda:
                                  not session.is_responsive(timeout=30),
                                  120, 0, 1):
            raise virt_vm.VMRebootError("Guest refuses to go down")
        session.close()

        error.context("logging in after reboot", logging.info)
        return self.wait_for_login(nic_index, timeout=timeout)


    def needs_restart(self, name, params, basedir):
        """
        Verifies whether the current qemu commandline matches the requested
        one, based on the test parameters.
        """
        return (self.__make_libvirt_command() !=
                self.__make_libvirt_command(name, params, basedir))


    def screendump(self, filename, debug=False):
        pass


    def send_key(self):
        pass


    def migrate(self):
        pass


    def pause(self):
        """
        Pause the VM operation.
        """
        self.domain.suspend()


    def resume(self):
        """
        Resume the VM operation in case it's stopped.
        """
        self.domain.resume()


    def save_to_file(self):
        pass


    def domain(self):
        if self.dom is not None:
            return self.dom
        
        if self.connect is None:
            self.connect = libvirt.open(None)
            if self.connect is None:
                raise libvirt.libvirtError('Failed connect to hypervisor')
                return self.dom

        self.dom = self.connect.lookupByName(self.name)
        if self.dom is None:
            raise libvirt.libvirtError('Domaint "%s" not found' % self.name)
        
        return self.dom