#
# installtree.py
#
# Copyright (C) 2010  Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Red Hat Author(s):  Martin Gracik <mgracik@redhat.com>
#

import logging
logger = logging.getLogger("pylorax.installtree")

import sys
import os
import shutil
import gzip
import lzma
import re
import glob
import time
import subprocess
import operator

from base import BaseLoraxClass, DataHolder
import constants
from sysutils import *


class LoraxInstallTree(BaseLoraxClass):

    def __init__(self, yum, basearch, libdir, workdir):
        BaseLoraxClass.__init__(self)
        self.yum = yum
        self.root = self.yum.installroot
        self.basearch = basearch
        self.libdir = libdir
        self.workdir = workdir

        self.lcmds = constants.LoraxRequiredCommands()

    def remove_locales(self):
        chroot = lambda: os.chroot(self.root)

        # get locales we need to keep
        langtable = joinpaths(self.root, "usr/share/anaconda/lang-table")
        if not os.path.exists(langtable):
            logger.critical("could not find anaconda lang-table, exiting")
            sys.exit(1)

        with open(langtable, "r") as fobj:
            langs = fobj.readlines()

        langs = map(lambda l: l.split()[3].replace(".UTF-8", ".utf8"), langs)
        langs = set(langs)

        # get locales from locale-archive
        localearch = "/usr/lib/locale/locale-archive"

        cmd = [self.lcmds.LOCALEDEF, "--list-archive", localearch]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, preexec_fn=chroot)
        output = proc.stdout.read()

        remove = set(output.split()) - langs

        # remove not needed locales
        cmd = [self.lcmds.LOCALEDEF, "-i", localearch,
               "--delete-from-archive"] + list(remove)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, preexec_fn=chroot)
        proc.wait()

        localearch = joinpaths(self.root, localearch)
        shutil.move(localearch, localearch + ".tmpl")

        proc = subprocess.Popen([self.lcmds.BUILD_LOCALE_ARCHIVE],
                             preexec_fn=chroot)
        proc.wait()

        # remove unneeded locales from /usr/share/locale
        with open(langtable, "r") as fobj:
            langs = fobj.readlines()

        langs = map(lambda l: l.split()[1], langs)

        localedir = joinpaths(self.root, "usr/share/locale")
        for fname in os.listdir(localedir):
            fpath = joinpaths(localedir, fname)
            if os.path.isdir(fpath) and fname not in langs:
                shutil.rmtree(fpath)

        # move the lang-table to etc
        shutil.move(langtable, joinpaths(self.root, "etc"))

    def create_keymaps(self):
        if self.basearch in ("s390", "s390x"):
            # skip on s390
            return True

        keymaps = joinpaths(self.root, "etc/keymaps.gz")

        # look for override
        override = "keymaps-override-{0.basearch}".format(self)
        override = joinpaths(self.root, "usr/share/anaconda", override)
        if os.path.isfile(override):
            logger.debug("using keymaps override")
            shutil.move(override, keymaps)
        else:
            # create keymaps
            cmd = [joinpaths(self.root, "usr/share/anaconda", "getkeymaps"),
                   self.basearch, keymaps, self.root]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
            proc.wait()

        return True

    def create_screenfont(self):
        dst = joinpaths(self.root, "etc/screenfont.gz")

        screenfont = "screenfont-{0.basearch}.gz".format(self)
        screenfont = joinpaths(self.root, "usr/share/anaconda", screenfont)
        if not os.path.isfile(screenfont):
            return False
        else:
            shutil.move(screenfont, dst)

        return True

    def move_stubs(self):
        stubs = ("list-harddrives", "loadkeys", "mknod",
                 "raidstart", "raidstop")

        for stub in stubs:
            src = joinpaths(self.root, "usr/share/anaconda",
                            "{0}-stub".format(stub))
            dst = joinpaths(self.root, "usr/bin", stub)
            if os.path.isfile(src):
                shutil.move(src, dst)

        # move restart-anaconda
        src = joinpaths(self.root, "usr/share/anaconda", "restart-anaconda")
        dst = joinpaths(self.root, "usr/bin")
        shutil.move(src, dst)

        # move sitecustomize.py
        pythonpath = joinpaths(self.root, "usr", self.libdir, "python?.?")
        for path in glob.glob(pythonpath):
            src = joinpaths(path, "site-packages/pyanaconda/sitecustomize.py")
            dst = joinpaths(path, "site-packages")
            shutil.move(src, dst)

    def cleanup_python_files(self):
        for root, _, fnames in os.walk(self.root):
            for fname in fnames:
                if fname.endswith(".py"):
                    path = joinpaths(root, fname, follow_symlinks=False)
                    pyo, pyc = path + "o", path + "c"
                    if os.path.isfile(pyo):
                        os.unlink(pyo)
                    if os.path.isfile(pyc):
                        os.unlink(pyc)

                    os.symlink("/dev/null", pyc)

    def move_modules(self):
        shutil.move(joinpaths(self.root, "lib/modules"),
                    joinpaths(self.root, "modules"))
        shutil.move(joinpaths(self.root, "lib/firmware"),
                    joinpaths(self.root, "firmware"))

        os.symlink("../modules", joinpaths(self.root, "lib/modules"))
        os.symlink("../firmware", joinpaths(self.root, "lib/firmware"))

    def cleanup_kernel_modules(self, keepmodules, kernel):
        moddir = joinpaths(self.root, "modules", kernel.version)
        fwdir = joinpaths(self.root, "firmware")

        # expand required modules
        modules = set()
        pattern = re.compile(r"\.ko$")

        for name in keepmodules:
            if name.startswith("="):
                group = name[1:]
                if group in ("scsi", "ata"):
                    mpath = joinpaths(moddir, "modules.block")
                elif group == "net":
                    mpath = joinpaths(moddir, "modules.networking")
                else:
                    mpath = joinpaths(moddir, "modules.{0}".format(group))

                if os.path.isfile(mpath):
                    with open(mpath, "r") as fobj:
                        for line in fobj:
                            module = pattern.sub("", line.strip())
                            modules.add(module)
            else:
                modules.add(name)

        # resolve modules dependencies
        moddep = joinpaths(moddir, "modules.dep")
        with open(moddep, "r") as fobj:
            lines = map(lambda line: line.strip(), fobj.readlines())

        modpattern = re.compile(r"^.*/(?P<name>.*)\.ko:(?P<deps>.*)$")
        deppattern = re.compile(r"^.*/(?P<name>.*)\.ko$")
        unresolved = True

        while unresolved:
            unresolved = False
            for line in lines:
                match = modpattern.match(line)
                modname = match.group("name")
                if modname in modules:
                    # add the dependencies
                    for dep in match.group("deps").split():
                        match = deppattern.match(dep)
                        depname = match.group("name")
                        if depname not in modules:
                            unresolved = True
                            modules.add(depname)

        # required firmware
        firmware = set()
        firmware.add("atmel_at76c504c-wpa.bin")
        firmware.add("iwlwifi-3945-1.ucode")
        firmware.add("iwlwifi-3945.ucode")
        firmware.add("zd1211/zd1211_uph")
        firmware.add("zd1211/zd1211_uphm")
        firmware.add("zd1211/zd1211b_uph")
        firmware.add("zd1211/zd1211b_uphm")

        # remove not needed modules
        for root, _, fnames in os.walk(moddir):
            for fname in fnames:
                path = os.path.join(root, fname)
                name, ext = os.path.splitext(fname)

                if ext == ".ko":
                    if name not in modules:
                        os.unlink(path)
                        logger.debug("removed module {0}".format(path))
                    else:
                        # get the required firmware
                        cmd = [self.lcmds.MODINFO, "-F", "firmware", path]
                        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
                        output = proc.stdout.read()
                        firmware |= set(output.split())

        # remove not needed firmware
        firmware = map(lambda fw: joinpaths(fwdir, fw), list(firmware))
        for root, _, fnames in os.walk(fwdir):
            for fname in fnames:
                path = joinpaths(root, fname)
                if path not in firmware:
                    os.unlink(path)
                    logger.debug("removed firmware {0}".format(path))

        # get the modules paths
        modpaths = {}
        for root, _, fnames in os.walk(moddir):
            for fname in fnames:
                modpaths[fname] = joinpaths(root, fname)

        # create the modules list
        modlist = {}
        for modtype, fname in (("scsi", "modules.block"),
                               ("eth", "modules.networking")):

            fname = joinpaths(moddir, fname)
            with open(fname, "r") as fobj:
                lines = map(lambda l: l.strip(), fobj.readlines())
                lines = filter(lambda l: l, lines)

            for line in lines:
                modname, ext = os.path.splitext(line)
                if (line not in modpaths or
                    modname in ("floppy", "libiscsi", "scsi_mod")):
                    continue

                cmd = [self.lcmds.MODINFO, "-F", "description", modpaths[line]]
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
                output = proc.stdout.read()

                try:
                    desc = output.splitlines()[0]
                    desc = desc.strip()[:65]
                except IndexError:
                    desc = "{0} driver".format(modname)

                info = '{0}\n\t{1}\n\t"{2}"\n'
                info = info.format(modname, modtype, desc)
                modlist[modname] = info

        # write the module-info
        moduleinfo = joinpaths(os.path.dirname(moddir), "module-info")
        with open(moduleinfo, "w") as fobj:
            fobj.write("Version 0\n")
            for modname in sorted(modlist.keys()):
                fobj.write(modlist[modname])

    def compress_modules(self, kernel):
        moddir = joinpaths(self.root, "modules", kernel.version)

        for root, _, fnames in os.walk(moddir):
            for fname in filter(lambda f: f.endswith(".ko"), fnames):
                path = os.path.join(root, fname)
                with open(path, "rb") as fobj:
                    data = fobj.read()

                gzipped = gzip.open("{0}.gz".format(path), "wb")
                gzipped.write(data)
                gzipped.close()

                os.unlink(path)

    def run_depmod(self, kernel):
        systemmap = "System.map-{0.version}".format(kernel)
        systemmap = joinpaths(self.root, "boot", systemmap)

        cmd = [self.lcmds.DEPMOD, "-a", "-F", systemmap, "-b", self.root,
               kernel.version]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        retcode = proc.wait()
        if not retcode == 0:
            logger.critical(proc.stdout.read())
            sys.exit(1)

        moddir = joinpaths(self.root, "modules", kernel.version)

        # remove *map files
        mapfiles = joinpaths(moddir, "*map")
        for fpath in glob.glob(mapfiles):
            os.unlink(fpath)

        # remove build and source symlinks
        for fname in ["build", "source"]:
            os.unlink(joinpaths(moddir, fname))

        # move modules out of the tree
        shutil.move(moddir, self.workdir)

    def move_repos(self):
        src = joinpaths(self.root, "etc/yum.repos.d")
        dst = joinpaths(self.root, "etc/anaconda.repos.d")
        shutil.move(src, dst)

    def create_depmod_conf(self):
        text = "search updates built-in\n"

        with open(joinpaths(self.root, "etc/depmod.d/dd.conf"), "w") as fobj:
            fobj.write(text)

    def misc_tree_modifications(self):
        if self.basearch in ("s390", "s390x"):
            # copy shutdown
            src = joinpaths(self.root, "usr", self.libdir, "anaconda/shutdown")
            dst = joinpaths(self.root, "sbin", "shutdown")
            os.unlink(dst)
            shutil.copy2(src, dst)

            # copy linuxrc.s390
            src = joinpaths(self.root, "usr/share/anaconda/linuxrc.s390")
            dst = joinpaths(self.root, "sbin", "init")
            os.unlink(dst)
            shutil.copy2(src, dst)
        else:
            # replace init with anaconda init
            src = joinpaths(self.root, "usr", self.libdir, "anaconda", "init")
            dst = joinpaths(self.root, "sbin", "init")
            os.unlink(dst)
            shutil.copy2(src, dst)

        # init symlinks
        target = "/sbin/init"
        name = joinpaths(self.root, "init")
        os.symlink(target, name)

        for fname in ["halt", "poweroff", "reboot"]:
            name = joinpaths(self.root, "sbin", fname)
            os.unlink(name)
            os.symlink("init", name)

        for fname in ["runlevel", "shutdown", "telinit"]:
            name = joinpaths(self.root, "sbin", fname)
            os.unlink(name)

        # mtab symlink
        #target = "/proc/mounts"
        #name = joinpaths(self.root, "etc", "mtab")
        #os.symlink(target, name)

        # create resolv.conf
        touch(joinpaths(self.root, "etc", "resolv.conf"))

    def get_config_files(self, src_dir):
        # anaconda needs to change a couple of the default gconf entries
        gconf = joinpaths(self.root, "etc", "gconf", "gconf.xml.defaults")

        # 0 - path, 1 - entry type, 2 - value
        gconf_settings = \
        [("/apps/metacity/general/button_layout", "string", ":"),
         ("/apps/metacity/general/action_right_click_titlebar",
          "string", "none"),
         ("/apps/metacity/general/num_workspaces", "int", "1"),
         ("/apps/metacity/window_keybindings/close", "string", "disabled"),
         ("/apps/metacity/global_keybindings/run_command_window_screenshot",
          "string", "disabled"),
         ("/apps/metacity/global_keybindings/run_command_screenshot",
          "string", "disabled"),
         ("/apps/metacity/global_keybindings/switch_to_workspace_down",
          "string", "disabled"),
         ("/apps/metacity/global_keybindings/switch_to_workspace_left",
          "string", "disabled"),
         ("/apps/metacity/global_keybindings/switch_to_workspace_right",
          "string", "disabled"),
         ("/apps/metacity/global_keybindings/switch_to_workspace_up",
          "string", "disabled"),
         ("/desktop/gnome/interface/accessibility", "bool", "true"),
         ("/desktop/gnome/interface/at-spi-corba", "bool", "true")]

        for path, entry_type, value in gconf_settings:
            cmd = [self.lcmds.GCONFTOOL, "--direct",
                   "--config-source=xml:readwrite:{0}".format(gconf),
                   "-s", "-t", entry_type, path, value]

            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
            proc.wait()

        # get rsyslog config
        src = joinpaths(src_dir, "rsyslog.conf")
        dst = joinpaths(self.root, "etc")
        shutil.copy2(src, dst)

        # get .bash_history
        src = joinpaths(src_dir, ".bash_history")
        dst = joinpaths(self.root, "root")
        shutil.copy2(src, dst)

        # get .profile
        src = joinpaths(src_dir, ".profile")
        dst = joinpaths(self.root, "root")
        shutil.copy2(src, dst)

        # get libuser.conf
        src = joinpaths(src_dir, "libuser.conf")
        dst = joinpaths(self.root, "etc")
        shutil.copy2(src, dst)

        # get selinux config
        if os.path.exists(joinpaths(self.root, "etc/selinux/targeted")):
            src = joinpaths(src_dir, "selinux.config")
            dst = joinpaths(self.root, "etc/selinux", "config")
            shutil.copy2(src, dst)

    def setup_sshd(self, src_dir):
        # get sshd config
        src = joinpaths(src_dir, "sshd_config.anaconda")
        dst = joinpaths(self.root, "etc", "ssh")
        shutil.copy2(src, dst)

        src = joinpaths(src_dir, "pam.sshd")
        dst = joinpaths(self.root, "etc", "pam.d", "sshd")
        shutil.copy2(src, dst)

        dst = joinpaths(self.root, "etc", "pam.d", "login")
        shutil.copy2(src, dst)

        dst = joinpaths(self.root, "etc", "pam.d", "remote")
        shutil.copy2(src, dst)

        # enable root shell logins and
        # 'install' account that starts anaconda on login
        passwd = joinpaths(self.root, "etc", "passwd")
        with open(passwd, "a") as fobj:
            fobj.write("sshd:x:74:74:Privilege-separated "
                       "SSH:/var/empty/sshd:/sbin/nologin\n")
            fobj.write("install:x:0:0:root:/root:/sbin/loader\n")

        shadow = joinpaths(self.root, "etc", "shadow")
        with open(shadow, "w") as fobj:
            fobj.write("root::14438:0:99999:7:::\n")
            fobj.write("install::14438:0:99999:7:::\n")

        # change permissions
        chmod_(shadow, 400)

        # generate ssh keys for s390
        if self.basearch in ("s390", "s390x"):
            logger.info("generating SSH1 RSA host key")
            rsa1 = joinpaths(self.root, "etc/ssh/ssh_host_key")
            cmd = [self.lcmds.SSHKEYGEN, "-q", "-t", "rsa1", "-f", rsa1,
                   "-C", "", "-N", ""]
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            p.wait()

            logger.info("generating SSH2 RSA host key")
            rsa2 = joinpaths(self.root, "etc/ssh/ssh_host_rsa_key")
            cmd = [self.lcmds.SSHKEYGEN, "-q", "-t", "rsa", "-f", rsa2,
                   "-C", "", "-N", ""]
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            p.wait()

            logger.info("generating SSH2 DSA host key")
            dsa = joinpaths(self.root, "etc/ssh/ssh_host_dsa_key")
            cmd = [self.lcmds.SSHKEYGEN, "-q", "-t", "dsa", "-f", dsa,
                   "-C", "", "-N", ""]
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            p.wait()

            # change key file permissions
            for key in [rsa1, rsa2, dsa]:
                chmod_(key, 0600)
                chmod_(key + ".pub", 0644)


    def get_anaconda_portions(self):
        src = joinpaths(self.root, "usr", self.libdir, "anaconda", "loader")
        dst = joinpaths(self.root, "sbin")
        shutil.copy2(src, dst)

        src = joinpaths(self.root, "usr/share/anaconda", "loader.tr")
        dst = joinpaths(self.root, "etc")
        shutil.move(src, dst)

        src = joinpaths(self.root, "usr/libexec/anaconda", "auditd")
        dst = joinpaths(self.root, "sbin")
        shutil.copy2(src, dst)

    def compress(self, initrd, kernel, type="xz"):
        chdir = lambda: os.chdir(self.root)
        start = time.time()

        # move corresponding modules to the tree
        shutil.move(joinpaths(self.workdir, kernel.version),
                    joinpaths(self.root, "modules"))

        find = subprocess.Popen([self.lcmds.FIND, "."], stdout=subprocess.PIPE,
                                preexec_fn=chdir)

        cpio = subprocess.Popen([self.lcmds.CPIO, "--quiet", "-c", "-o"],
                                stdin=find.stdout, stdout=subprocess.PIPE,
                                preexec_fn=chdir)

        if type == "gzip":
            compressed = gzip.open(initrd.fpath, "wb")
        elif type == "xz":
            compressed = lzma.LZMAFile(initrd.fpath, "w",
                    options={"format":"xz", "level":9})

        compressed.write(cpio.stdout.read())
        compressed.close()

        # move modules out of the tree again
        shutil.move(joinpaths(self.root, "modules", kernel.version),
                    self.workdir)

        elapsed = time.time() - start

        return True, elapsed

    @property
    def kernels(self):
        kerneldir = "boot"
        if self.basearch == "ia64":
            kerneldir = "boot/efi/EFI/redhat"

        kerneldir = joinpaths(self.root, kerneldir)
        kpattern = re.compile(r"vmlinuz-(?P<ver>[-._0-9a-z]+?"
                              r"(?P<pae>(PAE)?)(?P<xen>(xen)?))$")

        kernels = []
        for fname in os.listdir(kerneldir):
            match = kpattern.match(fname)
            if match:
                ktype = constants.K_NORMAL
                if match.group("pae"):
                    ktype = constants.K_PAE
                elif match.group("xen"):
                    ktype = constants.K_XEN

                kernels.append(DataHolder(fname=fname,
                                          fpath=joinpaths(kerneldir, fname),
                                          version=match.group("ver"),
                                          ktype=ktype))

        kernels = sorted(kernels, key=operator.attrgetter("ktype"))
        return kernels