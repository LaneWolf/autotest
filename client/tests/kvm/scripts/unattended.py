#!/usr/bin/python
"""
Simple script to setup unattended installs on KVM guests.
"""
# -*- coding: utf-8 -*-
import os, sys, shutil, tempfile, re
import common


class SetupError(Exception):
    """
    Simple wrapper for the builtin Exception class.
    """
    pass


class UnattendedInstall(object):
    """
    Creates a floppy disk image that will contain a config file for unattended
    OS install. Optionally, sets up a PXE install server using qemu built in
    TFTP and DHCP servers to install a particular operating system. The
    parameters to the script are retrieved from environment variables.
    """
    def __init__(self):
        """
        Gets params from environment variables and sets class attributes.
        """
        script_dir = os.path.dirname(sys.modules[__name__].__file__)
        kvm_test_dir = os.path.abspath(os.path.join(script_dir, ".."))
        images_dir = os.path.join(kvm_test_dir, 'images')
        self.deps_dir = os.path.join(kvm_test_dir, 'deps')
        self.unattended_dir = os.path.join(kvm_test_dir, 'unattended')

        tftp_root = os.environ.get('KVM_TEST_tftp', '')
        if tftp_root:
            self.tftp_root = os.path.join(kvm_test_dir, tftp_root)
            if not os.path.isdir(self.tftp_root):
                os.makedirs(self.tftp_root)
        else:
            self.tftp_root = tftp_root

        self.kernel_args = os.environ.get('KVM_TEST_kernel_args', '')
        self.finish_program= os.environ.get('KVM_TEST_finish_program', '')
        cdrom_iso = os.environ.get('KVM_TEST_cdrom')
        self.unattended_file = os.environ.get('KVM_TEST_unattended_file')

        self.qemu_img_bin = os.environ.get('KVM_TEST_qemu_img_binary')
        if not os.path.isabs(self.qemu_img_bin):
            self.qemu_img_bin = os.path.join(kvm_test_dir, self.qemu_img_bin)
        self.cdrom_iso = os.path.join(kvm_test_dir, cdrom_iso)
        self.floppy_mount = tempfile.mkdtemp(prefix='floppy_', dir='/tmp')
        self.cdrom_mount = tempfile.mkdtemp(prefix='cdrom_', dir='/tmp')
        flopy_name = os.environ['KVM_TEST_floppy']
        self.floppy_img = os.path.join(kvm_test_dir, flopy_name)
        floppy_dir = os.path.dirname(self.floppy_img)
        if not os.path.isdir(floppy_dir):
            os.makedirs(floppy_dir)

        self.pxe_dir = os.environ.get('KVM_TEST_pxe_dir', '')
        self.pxe_image = os.environ.get('KVM_TEST_pxe_image', '')
        self.pxe_initrd = os.environ.get('KVM_TEST_pxe_initrd', '')


    def create_boot_floppy(self):
        """
        Prepares a boot floppy by creating a floppy image file, mounting it and
        copying an answer file (kickstarts for RH based distros, answer files
        for windows) to it. After that the image is umounted.
        """
        print "Creating boot floppy"

        if os.path.exists(self.floppy_img):
            os.remove(self.floppy_img)

        c_cmd = '%s create -f raw %s 1440k' % (self.qemu_img_bin,
                                               self.floppy_img)
        if os.system(c_cmd):
            raise SetupError('Could not create floppy image.')

        f_cmd = 'mkfs.msdos -s 1 %s' % self.floppy_img
        if os.system(f_cmd):
            raise SetupError('Error formatting floppy image.')

        try:
            m_cmd = 'mount -o loop %s %s' % (self.floppy_img, self.floppy_mount)
            if os.system(m_cmd):
                raise SetupError('Could not mount floppy image.')

            if self.unattended_file.endswith('.sif'):
                dest_fname = 'winnt.sif'
                setup_file = 'winnt.bat'
                setup_file_path = os.path.join(self.unattended_dir, setup_file)
                setup_file_dest = os.path.join(self.floppy_mount, setup_file)
                shutil.copyfile(setup_file_path, setup_file_dest)
            elif self.unattended_file.endswith('.ks'):
                # Red Hat kickstart install
                dest_fname = 'ks.cfg'
            elif self.unattended_file.endswith('.xml'):
                if  self.tftp_root is '':
                    # Windows unattended install
                    dest_fname = "autounattend.xml"
                else:
                    # SUSE autoyast install
                    dest_fname = "autoinst.xml"

            dest = os.path.join(self.floppy_mount, dest_fname)

            # Replace KVM_TEST_CDKEY (in the unattended file) with the cdkey
            # provided for this test
            unattended_contents = open(self.unattended_file).read()
            dummy_cdkey_re = r'\bKVM_TEST_CDKEY\b'
            real_cdkey = os.environ.get('KVM_TEST_cdkey')
            if re.search(dummy_cdkey_re, unattended_contents):
                if real_cdkey:
                    unattended_contents = re.sub(dummy_cdkey_re, real_cdkey,
                                                 unattended_contents)
                else:
                    print ("WARNING: 'cdkey' required but not specified for "
                           "this unattended installation")

            # Write the unattended file contents to 'dest'
            open(dest, 'w').write(unattended_contents)

            if self.finish_program:
                dest_fname = os.path.basename(self.finish_program)
                dest = os.path.join(self.floppy_mount, dest_fname)
                shutil.copyfile(self.finish_program, dest)

        finally:
            u_cmd = 'umount %s' % self.floppy_mount
            if os.system(u_cmd):
                raise SetupError('Could not unmount floppy at %s.' %
                                 self.floppy_mount)
            self.cleanup(self.floppy_mount)

        os.chmod(self.floppy_img, 0755)

        print "Boot floppy created successfuly"


    def setup_pxe_boot(self):
        """
        Sets up a PXE boot environment using the built in qemu TFTP server.
        Copies the PXE Linux bootloader pxelinux.0 from the host (needs the
        pxelinux package or equivalent for your distro), and vmlinuz and
        initrd.img files from the CD to a directory that qemu will serve trough
        TFTP to the VM.
        """
        print "Setting up PXE boot using TFTP root %s" % self.tftp_root

        pxe_file = None
        pxe_paths = ['/usr/lib/syslinux/pxelinux.0',
                     '/usr/share/syslinux/pxelinux.0']
        for path in pxe_paths:
            if os.path.isfile(path):
                pxe_file = path
                break

        if not pxe_file:
            raise SetupError('Cannot find PXE boot loader pxelinux.0. Make '
                             'sure pxelinux or equivalent package for your '
                             'distro is installed.')

        pxe_dest = os.path.join(self.tftp_root, 'pxelinux.0')
        shutil.copyfile(pxe_file, pxe_dest)

        try:
            m_cmd = 'mount -t iso9660 -v -o loop,ro %s %s' % (self.cdrom_iso,
                                                              self.cdrom_mount)
            if os.system(m_cmd):
                raise SetupError('Could not mount CD image %s.' %
                                 self.cdrom_iso)

            pxe_dir = os.path.join(self.cdrom_mount, self.pxe_dir)
            pxe_image = os.path.join(pxe_dir, self.pxe_image)
            pxe_initrd = os.path.join(pxe_dir, self.pxe_initrd)

            if not os.path.isdir(pxe_dir):
                raise SetupError('The ISO image does not have a %s dir. The '
                                 'script assumes that the cd has a %s dir '
                                 'where to search for the vmlinuz image.' %
                                 (self.pxe_dir, self.pxe_dir))

            if not os.path.isfile(pxe_image) or not os.path.isfile(pxe_initrd):
                raise SetupError('The location %s is lacking either a vmlinuz '
                                 'or a initrd.img file. Cannot find a PXE '
                                 'image to proceed.' % self.pxe_dir)

            tftp_image = os.path.join(self.tftp_root, 'vmlinuz')
            tftp_initrd = os.path.join(self.tftp_root, 'initrd.img')
            shutil.copyfile(pxe_image, tftp_image)
            shutil.copyfile(pxe_initrd, tftp_initrd)

        finally:
            u_cmd = 'umount %s' % self.cdrom_mount
            if os.system(u_cmd):
                raise SetupError('Could not unmount CD at %s.' %
                                 self.cdrom_mount)
            self.cleanup(self.cdrom_mount)

        pxe_config_dir = os.path.join(self.tftp_root, 'pxelinux.cfg')
        if not os.path.isdir(pxe_config_dir):
            os.makedirs(pxe_config_dir)
        pxe_config_path = os.path.join(pxe_config_dir, 'default')

        pxe_config = open(pxe_config_path, 'w')
        pxe_config.write('DEFAULT pxeboot\n')
        pxe_config.write('TIMEOUT 20\n')
        pxe_config.write('PROMPT 0\n')
        pxe_config.write('LABEL pxeboot\n')
        pxe_config.write('     KERNEL vmlinuz\n')
        pxe_config.write('     KERNEL vmlinuz\n')
        pxe_config.write('     APPEND initrd=initrd.img %s\n' %
                         self.kernel_args)
        pxe_config.close()

        print "PXE boot successfuly set"


    def cleanup(self, mount):
        """
        Clean up a previously used mountpoint.

        @param mount: Mountpoint to be cleaned up.
        """
        if os.path.isdir(mount):
            if os.path.ismount(mount):
                print "Path %s is still mounted, please verify" % mount
            else:
                print "Removing mount point %s" % mount
                os.rmdir(mount)


    def setup(self):
        print "Starting unattended install setup"

        print "Variables set:"
        print "    qemu_img_bin: " + str(self.qemu_img_bin)
        print "    cdrom iso: " + str(self.cdrom_iso)
        print "    unattended_file: " + str(self.unattended_file)
        print "    kernel_args: " + str(self.kernel_args)
        print "    tftp_root: " + str(self.tftp_root)
        print "    floppy_mount: " + str(self.floppy_mount)
        print "    floppy_img: " + str(self.floppy_img)
        print "    finish_program: " + str(self.finish_program)
        print "    pxe_dir: " + str(self.pxe_dir)
        print "    pxe_image: " + str(self.pxe_image)
        print "    pxe_initrd: " + str(self.pxe_initrd)

        self.create_boot_floppy()
        if self.tftp_root:
            self.setup_pxe_boot()
        print "Unattended install setup finished successfuly"


if __name__ == "__main__":
    os_install = UnattendedInstall()
    os_install.setup()
