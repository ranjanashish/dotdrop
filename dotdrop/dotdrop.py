"""
author: deadc0de6 (https://github.com/deadc0de6)
Copyright (c) 2017, deadc0de6

entry point
"""

import os
import sys
import time
from concurrent import futures
import shutil

# local imports
from dotdrop.options import Options
from dotdrop.logger import Logger
from dotdrop.templategen import Templategen
from dotdrop.installer import Installer
from dotdrop.updater import Updater
from dotdrop.comparator import Comparator
from dotdrop.utils import get_tmpdir, removepath, strip_home, \
    uniq_list, patch_ignores, dependencies_met
from dotdrop.linktypes import LinkTypes
from dotdrop.exceptions import YamlException, UndefinedException

LOG = Logger()
TRANS_SUFFIX = 'trans'

###########################################################
# entry point
###########################################################


def action_executor(o, actions, defactions, templater, post=False):
    """closure for action execution"""
    def execute():
        """
        execute actions and return
        True, None if ok
        False, errstring if issue
        """
        s = 'pre' if not post else 'post'

        # execute default actions
        for action in defactions:
            if o.dry:
                LOG.dry('would execute def-{}-action: {}'.format(s,
                                                                 action))
                continue
            if o.debug:
                LOG.dbg('executing def-{}-action: {}'.format(s, action))
            ret = action.execute(templater=templater, debug=o.debug)
            if not ret:
                err = 'def-{}-action \"{}\" failed'.format(s, action.key)
                LOG.err(err)
                return False, err

        # execute actions
        for action in actions:
            if o.dry:
                LOG.dry('would execute {}-action: {}'.format(s, action))
                continue
            if o.debug:
                LOG.dbg('executing {}-action: {}'.format(s, action))
            ret = action.execute(templater=templater, debug=o.debug)
            if not ret:
                err = '{}-action \"{}\" failed'.format(s, action.key)
                LOG.err(err)
                return False, err
        return True, None
    return execute


def _dotfile_install(o, dotfile, tmpdir=None):
    """
    install a dotfile
    returns <success, dotfile key, err>
    """
    # installer
    inst = _get_install_installer(o, tmpdir=tmpdir)

    # templater
    t = _get_templater(o)

    # add dotfile variables
    newvars = dotfile.get_dotfile_variables()
    t.add_tmp_vars(newvars=newvars)

    preactions = []
    if not o.install_temporary:
        preactions.extend(dotfile.get_pre_actions())
    defactions = o.install_default_actions_pre
    pre_actions_exec = action_executor(o, preactions, defactions,
                                       t, post=False)

    if o.debug:
        LOG.dbg('installing dotfile: \"{}\"'.format(dotfile.key))
        LOG.dbg(dotfile.prt())

    if hasattr(dotfile, 'link') and dotfile.link == LinkTypes.LINK:
        # link
        r, err = inst.link(t, dotfile.src, dotfile.dst,
                           actionexec=pre_actions_exec,
                           template=dotfile.template)
    elif hasattr(dotfile, 'link') and \
            dotfile.link == LinkTypes.LINK_CHILDREN:
        # link_children
        r, err = inst.link_children(t, dotfile.src, dotfile.dst,
                                    actionexec=pre_actions_exec,
                                    template=dotfile.template)
    else:
        # nolink
        src = dotfile.src
        tmp = None
        if dotfile.trans_r:
            tmp = apply_trans(o.dotpath, dotfile, t, debug=o.debug)
            if not tmp:
                return False, dotfile.key, None
            src = tmp
        ignores = list(set(o.install_ignore + dotfile.instignore))
        ignores = patch_ignores(ignores, dotfile.dst, debug=o.debug)
        r, err = inst.install(t, src, dotfile.dst,
                              actionexec=pre_actions_exec,
                              noempty=dotfile.noempty,
                              ignore=ignores,
                              template=dotfile.template)
        if tmp:
            tmp = os.path.join(o.dotpath, tmp)
            if os.path.exists(tmp):
                removepath(tmp, LOG)

    # check result of installation
    if r:
        # dotfile was installed
        if not o.install_temporary:
            defactions = o.install_default_actions_post
            postactions = dotfile.get_post_actions()
            post_actions_exec = action_executor(o, postactions, defactions,
                                                t, post=True)
            post_actions_exec()
    else:
        # dotfile was NOT installed
        if o.install_force_action:
            # pre-actions
            if o.debug:
                LOG.dbg('force pre action execution ...')
            pre_actions_exec()
            # post-actions
            if o.debug:
                LOG.dbg('force post action execution ...')
            defactions = o.install_default_actions_post
            postactions = dotfile.get_post_actions()
            post_actions_exec = action_executor(o, postactions, defactions,
                                                t, post=True)
            post_actions_exec()

    return r, dotfile.key, err


def cmd_install(o):
    """install dotfiles for this profile"""
    dotfiles = o.dotfiles
    prof = o.conf.get_profile()
    pro_pre_actions = prof.get_pre_actions() if prof else []
    pro_post_actions = prof.get_post_actions() if prof else []

    if o.install_keys:
        # filtered dotfiles to install
        uniq = uniq_list(o.install_keys)
        dotfiles = [d for d in dotfiles if d.key in uniq]
    if not dotfiles:
        msg = 'no dotfile to install for this profile (\"{}\")'
        LOG.warn(msg.format(o.profile))
        return False

    # the installer
    tmpdir = None
    if o.install_temporary:
        tmpdir = get_tmpdir()

    installed = 0

    # execute profile pre-action
    if o.debug:
        LOG.dbg('run {} profile pre actions'.format(len(pro_pre_actions)))
    t = _get_templater(o)
    ret, err = action_executor(o, pro_pre_actions, [], t, post=False)()
    if not ret:
        return False

    # install each dotfile
    if o.install_parallel > 1:
        # in parallel
        ex = futures.ThreadPoolExecutor(max_workers=o.install_parallel)

        wait_for = [
            ex.submit(_dotfile_install, o, dotfile, tmpdir=tmpdir)
            for dotfile in dotfiles
        ]
        for f in futures.as_completed(wait_for):
            r, key, err = f.result()
            if r:
                installed += 1
            elif err:
                LOG.err('installing \"{}\" failed: {}'.format(key,
                                                              err))
    else:
        # sequentially
        for dotfile in dotfiles:
            r, key, err = _dotfile_install(o, dotfile, tmpdir=tmpdir)
            if r:
                installed += 1
            elif err:
                LOG.err('installing \"{}\" failed: {}'.format(key,
                                                              err))

    # execute profile post-action
    if installed > 0 or o.install_force_action:
        if o.debug:
            msg = 'run {} profile post actions'
            LOG.dbg(msg.format(len(pro_post_actions)))
        ret, err = action_executor(o, pro_post_actions, [], t, post=False)()
        if not ret:
            return False

    if o.debug:
        LOG.dbg('install done - {} installed'.format(installed))

    if o.install_temporary:
        LOG.log('\ninstalled to tmp \"{}\".'.format(tmpdir))
    LOG.log('\n{} dotfile(s) installed.'.format(installed))
    return True


def cmd_compare(o, tmp):
    """compare dotfiles and return True if all identical"""
    dotfiles = o.dotfiles
    if not dotfiles:
        msg = 'no dotfile defined for this profile (\"{}\")'
        LOG.warn(msg.format(o.profile))
        return True
    # compare only specific files
    same = True
    selected = dotfiles
    if o.compare_focus:
        selected = _select(o.compare_focus, dotfiles)

    if len(selected) < 1:
        return False

    t = _get_templater(o)
    tvars = t.add_tmp_vars()
    inst = Installer(create=o.create, backup=o.backup,
                     dry=o.dry, base=o.dotpath,
                     workdir=o.workdir, debug=o.debug,
                     backup_suffix=o.install_backup_suffix,
                     diff_cmd=o.diff_command)
    comp = Comparator(diff_cmd=o.diff_command, debug=o.debug)

    for dotfile in selected:
        if not dotfile.src and not dotfile.dst:
            # ignore fake dotfile
            continue
        # add dotfile variables
        t.restore_vars(tvars)
        newvars = dotfile.get_dotfile_variables()
        t.add_tmp_vars(newvars=newvars)

        # dotfiles does not exist / not installed
        if o.debug:
            LOG.dbg('comparing {}'.format(dotfile))
        src = dotfile.src
        if not os.path.lexists(os.path.expanduser(dotfile.dst)):
            line = '=> compare {}: \"{}\" does not exist on destination'
            LOG.log(line.format(dotfile.key, dotfile.dst))
            same = False
            continue

        # apply transformation
        tmpsrc = None
        if dotfile.trans_r:
            if o.debug:
                LOG.dbg('applying transformation before comparing')
            tmpsrc = apply_trans(o.dotpath, dotfile, t, debug=o.debug)
            if not tmpsrc:
                # could not apply trans
                same = False
                continue
            src = tmpsrc

        # is a symlink pointing to itself
        asrc = os.path.join(o.dotpath, os.path.expanduser(src))
        adst = os.path.expanduser(dotfile.dst)
        if os.path.samefile(asrc, adst):
            if o.debug:
                line = '=> compare {}: diffing with \"{}\"'
                LOG.dbg(line.format(dotfile.key, dotfile.dst))
                LOG.dbg('points to itself')
            continue

        # install dotfile to temporary dir and compare
        ret, err, insttmp = inst.install_to_temp(t, tmp, src, dotfile.dst,
                                                 template=dotfile.template)
        if not ret:
            # failed to install to tmp
            line = '=> compare {}: error'
            LOG.log(line.format(dotfile.key, err))
            LOG.err(err)
            same = False
            continue
        ignores = list(set(o.compare_ignore + dotfile.cmpignore))
        ignores = patch_ignores(ignores, dotfile.dst, debug=o.debug)
        diff = comp.compare(insttmp, dotfile.dst, ignore=ignores)

        # clean tmp transformed dotfile if any
        if tmpsrc:
            tmpsrc = os.path.join(o.dotpath, tmpsrc)
            if os.path.exists(tmpsrc):
                removepath(tmpsrc, LOG)

        if diff == '':
            # no difference
            if o.debug:
                line = '=> compare {}: diffing with \"{}\"'
                LOG.dbg(line.format(dotfile.key, dotfile.dst))
                LOG.dbg('same file')
        else:
            # print diff results
            line = '=> compare {}: diffing with \"{}\"'
            LOG.log(line.format(dotfile.key, dotfile.dst))
            if o.compare_fileonly:
                LOG.raw('<files are different>')
            else:
                LOG.emph(diff)
            same = False

    return same


def cmd_update(o):
    """update the dotfile(s) from path(s) or key(s)"""
    ret = True
    paths = o.update_path
    iskey = o.update_iskey
    ignore = o.update_ignore
    showpatch = o.update_showpatch

    if not paths:
        # update the entire profile
        if iskey:
            paths = [d.key for d in o.dotfiles]
        else:
            paths = [d.dst for d in o.dotfiles]
        msg = 'Update all dotfiles for profile \"{}\"'.format(o.profile)
        if o.safe and not LOG.ask(msg):
            return False

    if not paths:
        LOG.log('no dotfile to update')
        return True
    if o.debug:
        LOG.dbg('dotfile to update: {}'.format(paths))

    updater = Updater(o.dotpath, o.variables,
                      o.conf.get_dotfile,
                      o.conf.get_dotfile_by_dst,
                      o.conf.path_to_dotfile_dst,
                      dry=o.dry, safe=o.safe, debug=o.debug,
                      ignore=ignore, showpatch=showpatch)
    if not iskey:
        # update paths
        if o.debug:
            LOG.dbg('update by paths: {}'.format(paths))
        for path in paths:
            if not updater.update_path(path):
                ret = False
    else:
        # update keys
        keys = paths
        if not keys:
            # if not provided, take all keys
            keys = [d.key for d in o.dotfiles]
        if o.debug:
            LOG.dbg('update by keys: {}'.format(keys))
        for key in keys:
            if not updater.update_key(key):
                ret = False
    return ret


def cmd_importer(o):
    """import dotfile(s) from paths"""
    ret = True
    cnt = 0
    paths = o.import_path
    for path in paths:
        if o.debug:
            LOG.dbg('trying to import {}'.format(path))
        if not os.path.exists(path):
            LOG.err('\"{}\" does not exist, ignored!'.format(path))
            ret = False
            continue
        dst = path.rstrip(os.sep)
        dst = os.path.abspath(dst)

        if o.safe:
            # ask for symlinks
            realdst = os.path.realpath(dst)
            if dst != realdst:
                msg = '\"{}\" is a symlink, dereference it and continue?'
                if not LOG.ask(msg.format(dst)):
                    continue

        src = strip_home(dst)
        if o.import_as:
            # handle import as
            src = os.path.expanduser(o.import_as)
            src = src.rstrip(os.sep)
            src = os.path.abspath(src)
            src = strip_home(src)
            if o.debug:
                LOG.dbg('import src for {} as {}'.format(dst, src))

        strip = '.' + os.sep
        if o.keepdot:
            strip = os.sep
        src = src.lstrip(strip)

        # set the link attribute
        linktype = o.import_link
        if linktype == LinkTypes.LINK_CHILDREN and \
                not os.path.isdir(path):
            LOG.err('importing \"{}\" failed!'.format(path))
            ret = False
            continue

        if o.debug:
            LOG.dbg('import dotfile: src:{} dst:{}'.format(src, dst))

        # test no other dotfile exists with same
        # dst for this profile but different src
        dfs = o.conf.get_dotfile_by_dst(dst)
        if dfs:
            invalid = False
            for df in dfs:
                profiles = o.conf.get_profiles_by_dotfile_key(df.key)
                profiles = [x.key for x in profiles]
                if o.profile in profiles and \
                        not o.conf.get_dotfile_by_src_dst(src, dst):
                    # same profile
                    # different src
                    LOG.err('duplicate dotfile for this profile')
                    ret = False
                    invalid = True
                    break
            if invalid:
                continue

        # prepare hierarchy for dotfile
        srcf = os.path.join(o.dotpath, src)
        overwrite = not os.path.exists(srcf)
        if os.path.exists(srcf):
            overwrite = True
            if o.safe:
                c = Comparator(debug=o.debug, diff_cmd=o.diff_command)
                diff = c.compare(srcf, dst)
                if diff != '':
                    # files are different, dunno what to do
                    LOG.log('diff \"{}\" VS \"{}\"'.format(dst, srcf))
                    LOG.emph(diff)
                    # ask user
                    msg = 'Dotfile \"{}\" already exists, overwrite?'
                    overwrite = LOG.ask(msg.format(srcf))

        if o.debug:
            LOG.dbg('will overwrite: {}'.format(overwrite))
        if overwrite:
            cmd = 'mkdir -p {}'.format(os.path.dirname(srcf))
            if o.dry:
                LOG.dry('would run: {}'.format(cmd))
            else:
                try:
                    os.makedirs(os.path.dirname(srcf), exist_ok=True)
                except Exception:
                    LOG.err('importing \"{}\" failed!'.format(path))
                    ret = False
                    continue
            if o.dry:
                LOG.dry('would copy {} to {}'.format(dst, srcf))
            else:
                if os.path.isdir(dst):
                    if os.path.exists(srcf):
                        shutil.rmtree(srcf)
                    shutil.copytree(dst, srcf)
                else:
                    shutil.copy2(dst, srcf)
        retconf = o.conf.new(src, dst, linktype)
        if retconf:
            LOG.sub('\"{}\" imported'.format(path))
            cnt += 1
        else:
            LOG.warn('\"{}\" ignored'.format(path))
    if o.dry:
        LOG.dry('new config file would be:')
        LOG.raw(o.conf.dump())
    else:
        o.conf.save()
    LOG.log('\n{} file(s) imported.'.format(cnt))
    return ret


def cmd_list_profiles(o):
    """list all profiles"""
    LOG.emph('Available profile(s):\n')
    for p in o.profiles:
        if o.profiles_grepable:
            fmt = '{}'.format(p.key)
            LOG.raw(fmt)
        else:
            LOG.sub(p.key, end='')
            LOG.log(' ({} dotfiles)'.format(len(p.dotfiles)))
    LOG.log('')


def cmd_list_files(o):
    """list all dotfiles for a specific profile"""
    if o.profile not in [p.key for p in o.profiles]:
        LOG.warn('unknown profile \"{}\"'.format(o.profile))
        return
    what = 'Dotfile(s)'
    if o.files_templateonly:
        what = 'Template(s)'
    LOG.emph('{} for profile \"{}\":\n'.format(what, o.profile))
    for dotfile in o.dotfiles:
        if o.files_templateonly:
            src = os.path.join(o.dotpath, dotfile.src)
            if not Templategen.is_template(src):
                continue
        if o.files_grepable:
            fmt = '{},dst:{},src:{},link:{}'
            fmt = fmt.format(dotfile.key, dotfile.dst,
                             dotfile.src, dotfile.link.name.lower())
            LOG.raw(fmt)
        else:
            LOG.log('{}'.format(dotfile.key), bold=True)
            LOG.sub('dst: {}'.format(dotfile.dst))
            LOG.sub('src: {}'.format(dotfile.src))
            LOG.sub('link: {}'.format(dotfile.link.name.lower()))
    LOG.log('')


def cmd_detail(o):
    """list details on all files for all dotfile entries"""
    if o.profile not in [p.key for p in o.profiles]:
        LOG.warn('unknown profile \"{}\"'.format(o.profile))
        return
    dotfiles = o.dotfiles
    if o.detail_keys:
        # filtered dotfiles to install
        uniq = uniq_list(o.details_keys)
        dotfiles = [d for d in dotfiles if d.key in uniq]
    LOG.emph('dotfiles details for profile \"{}\":\n'.format(o.profile))
    for d in dotfiles:
        _detail(o.dotpath, d)
    LOG.log('')


def cmd_remove(o):
    """remove dotfile from dotpath and from config"""
    paths = o.remove_path
    iskey = o.remove_iskey

    if not paths:
        LOG.log('no dotfile to remove')
        return False
    if o.debug:
        LOG.dbg('dotfile(s) to remove: {}'.format(','.join(paths)))

    removed = []
    for key in paths:
        if not iskey:
            # by path
            dotfiles = o.conf.get_dotfile_by_dst(key)
            if not dotfiles:
                LOG.warn('{} ignored, does not exist'.format(key))
                continue
        else:
            # by key
            dotfile = o.conf.get_dotfile(key)
            if not dotfile:
                LOG.warn('{} ignored, does not exist'.format(key))
                continue
            dotfiles = [dotfile]

        for dotfile in dotfiles:
            k = dotfile.key
            # ignore if uses any type of link
            if dotfile.link != LinkTypes.NOLINK:
                LOG.warn('dotfile uses link, remove manually')
                continue

            if o.debug:
                LOG.dbg('removing {}'.format(key))

            # make sure is part of the profile
            if dotfile.key not in [d.key for d in o.dotfiles]:
                msg = '{} ignored, not associated to this profile'
                LOG.warn(msg.format(key))
                continue
            profiles = o.conf.get_profiles_by_dotfile_key(k)
            pkeys = ','.join([p.key for p in profiles])
            if o.dry:
                LOG.dry('would remove {} from {}'.format(dotfile, pkeys))
                continue
            msg = 'Remove \"{}\" from all these profiles: {}'.format(k, pkeys)
            if o.safe and not LOG.ask(msg):
                return False
            if o.debug:
                LOG.dbg('remove dotfile: {}'.format(dotfile))

            for profile in profiles:
                if not o.conf.del_dotfile_from_profile(dotfile, profile):
                    return False
            if not o.conf.del_dotfile(dotfile):
                return False

            # remove dotfile from dotpath
            dtpath = os.path.join(o.dotpath, dotfile.src)
            removepath(dtpath, LOG)
            # remove empty directory
            parent = os.path.dirname(dtpath)
            # remove any empty parent up to dotpath
            while parent != o.dotpath:
                if os.path.isdir(parent) and not os.listdir(parent):
                    msg = 'Remove empty dir \"{}\"'.format(parent)
                    if o.safe and not LOG.ask(msg):
                        break
                    removepath(parent, LOG)
                parent = os.path.dirname(parent)
            removed.append(dotfile.key)

    if o.dry:
        LOG.dry('new config file would be:')
        LOG.raw(o.conf.dump())
    else:
        o.conf.save()
    if removed:
        LOG.log('\ndotfile(s) removed: {}'.format(','.join(removed)))
    else:
        LOG.log('\nno dotfile removed')
    return True


###########################################################
# helpers
###########################################################


def _get_install_installer(o, tmpdir=None):
    """get an installer instance for cmd_install"""
    inst = Installer(create=o.create, backup=o.backup,
                     dry=o.dry, safe=o.safe,
                     base=o.dotpath, workdir=o.workdir,
                     diff=o.install_diff, debug=o.debug,
                     totemp=tmpdir,
                     showdiff=o.install_showdiff,
                     backup_suffix=o.install_backup_suffix,
                     diff_cmd=o.diff_command)
    return inst


def _get_templater(o):
    """get an templater instance"""
    t = Templategen(base=o.dotpath, variables=o.variables,
                    func_file=o.func_file, filter_file=o.filter_file,
                    debug=o.debug)
    return t


def _detail(dotpath, dotfile):
    """display details on all files under a dotfile entry"""
    LOG.log('{} (dst: \"{}\", link: {})'.format(dotfile.key, dotfile.dst,
                                                dotfile.link.name.lower()))
    path = os.path.join(dotpath, os.path.expanduser(dotfile.src))
    if not os.path.isdir(path):
        template = 'no'
        if Templategen.is_template(path):
            template = 'yes'
        LOG.sub('{} (template:{})'.format(path, template))
    else:
        for root, _, files in os.walk(path):
            for f in files:
                p = os.path.join(root, f)
                template = 'no'
                if Templategen.is_template(p):
                    template = 'yes'
                LOG.sub('{} (template:{})'.format(p, template))


def _select(selections, dotfiles):
    selected = []
    for selection in selections:
        df = next(
            (x for x in dotfiles
                if os.path.expanduser(x.dst) == os.path.expanduser(selection)),
            None
        )
        if df:
            selected.append(df)
        else:
            LOG.err('no dotfile matches \"{}\"'.format(selection))
    return selected


def apply_trans(dotpath, dotfile, templater, debug=False):
    """
    apply the read transformation to the dotfile
    return None if fails and new source if succeed
    """
    src = dotfile.src
    new_src = '{}.{}'.format(src, TRANS_SUFFIX)
    trans = dotfile.trans_r
    if debug:
        LOG.dbg('executing transformation: {}'.format(trans))
    s = os.path.join(dotpath, src)
    temp = os.path.join(dotpath, new_src)
    if not trans.transform(s, temp, templater=templater, debug=debug):
        msg = 'transformation \"{}\" failed for {}'
        LOG.err(msg.format(trans.key, dotfile.key))
        if new_src and os.path.exists(new_src):
            removepath(new_src, LOG)
        return None
    return new_src


###########################################################
# main
###########################################################


def main():
    """entry point"""
    # check dependencies are met
    try:
        dependencies_met()
    except Exception as e:
        LOG.err(e)
        return False

    t0 = time.time()
    try:
        o = Options()
    except YamlException as e:
        LOG.err('config error: {}'.format(str(e)))
        return False
    except UndefinedException as e:
        LOG.err('config error: {}'.format(str(e)))
        return False

    if o.debug:
        LOG.dbg('\n\n')
    options_time = time.time() - t0

    ret = True
    t0 = time.time()
    command = ''
    try:

        if o.cmd_profiles:
            # list existing profiles
            command = 'profiles'
            if o.debug:
                LOG.dbg('running cmd: {}'.format(command))
            cmd_list_profiles(o)

        elif o.cmd_files:
            # list files for selected profile
            command = 'files'
            if o.debug:
                LOG.dbg('running cmd: {}'.format(command))
            cmd_list_files(o)

        elif o.cmd_install:
            # install the dotfiles stored in dotdrop
            command = 'install'
            if o.debug:
                LOG.dbg('running cmd: {}'.format(command))
            ret = cmd_install(o)

        elif o.cmd_compare:
            # compare local dotfiles with dotfiles stored in dotdrop
            command = 'compare'
            if o.debug:
                LOG.dbg('running cmd: {}'.format(command))
            tmp = get_tmpdir()
            ret = cmd_compare(o, tmp)
            # clean tmp directory
            removepath(tmp, LOG)

        elif o.cmd_import:
            # import dotfile(s)
            command = 'import'
            if o.debug:
                LOG.dbg('running cmd: {}'.format(command))
            ret = cmd_importer(o)

        elif o.cmd_update:
            # update a dotfile
            command = 'update'
            if o.debug:
                LOG.dbg('running cmd: {}'.format(command))
            ret = cmd_update(o)

        elif o.cmd_detail:
            # detail files
            command = 'detail'
            if o.debug:
                LOG.dbg('running cmd: {}'.format(command))
            cmd_detail(o)

        elif o.cmd_remove:
            # remove dotfile
            command = 'remove'
            if o.debug:
                LOG.dbg('running cmd: {}'.format(command))
            cmd_remove(o)

    except KeyboardInterrupt:
        LOG.err('interrupted')
        ret = False
    cmd_time = time.time() - t0

    if o.debug:
        LOG.dbg('done executing command \"{}\"'.format(command))
        LOG.dbg('options loaded in {}'.format(options_time))
        LOG.dbg('command executed in {}'.format(cmd_time))

    if ret and o.conf.save():
        LOG.log('config file updated')

    if o.debug:
        LOG.dbg('return {}'.format(ret))
    return ret


if __name__ == '__main__':
    if main():
        sys.exit(0)
    sys.exit(1)
