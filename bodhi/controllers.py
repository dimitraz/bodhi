# $Id: controllers.py,v 1.11 2007/01/08 06:07:07 lmacken Exp $
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Library General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.

import rpm
import mail
import logging
import cherrypy

from koji import GenericError
from datetime import datetime
from sqlobject import SQLObjectNotFound
from sqlobject.sqlbuilder import AND, OR

from turbogears import (controllers, expose, validate, redirect, identity,
                        paginate, flash, error_handler, validators, config, url)
from turbogears.widgets import DataGrid

from bodhi import buildsys, util
from bodhi.rss import Feed
from bodhi.util import flash_log, get_pkg_pushers
from bodhi.new import NewUpdateController, update_form
from bodhi.admin import AdminController
from bodhi.model import (Package, PackageBuild, PackageUpdate, Release,
                         Bugzilla, CVE, Comment)
from bodhi.search import SearchController
from bodhi.widgets import CommentForm, OkCancelForm
from bodhi.exceptions import (DuplicateEntryError,
                              PostgresIntegrityError, SQLiteIntegrityError)

log = logging.getLogger(__name__)


class Root(controllers.RootController):

    new = NewUpdateController()
    admin = AdminController()
    search = SearchController()
    rss = Feed("rss2.0")

    comment_form = CommentForm()
    ok_cancel_form = OkCancelForm()

    def exception(self, tg_exceptions=None):
        """ Generic exception handler """
        log.error("Exception thrown: %s" % str(tg_exceptions))
        flash_log(str(tg_exceptions))
        if 'tg_format' in cherrypy.request.params and \
                cherrypy.request.params['tg_format'] == 'json':
            return dict()
        raise redirect("/")

    def jsonRequest(self):
        return 'tg_format' in cherrypy.request.params and \
                cherrypy.request.params['tg_format'] == 'json'

    @identity.require(identity.not_anonymous())
    @expose(template='bodhi.templates.welcome')
    def index(self):
        """
        The main dashboard.  Here we generate the DataGrids for My Updates and 
        the latest comments.
        """
        from bodhi.util import make_update_link, make_type_icon, make_karma_icon
        RESULTS, FIELDS, GRID = range(3)

        # { 'Title' : [SelectResults, [(row, row_callback),]], ... }
        grids = {
            'comments' : [
                Comment.select(orderBy=Comment.q.timestamp).reversed(),
                [
                    ('Update', make_update_link),
                    ('Comment', lambda row: row.text),
                    ('From', lambda row: row.author),
                    ('Karma', make_karma_icon)
                ]
            ],
            'mine' : [
                PackageUpdate.select(
                    PackageUpdate.q.submitter == identity.current.user_name,
                    orderBy=PackageUpdate.q.date_pushed
                ).reversed(),
                [
                    ('Update', make_update_link),
                    ('Status', lambda row: row.status),
                    ('Type', make_type_icon),
                    ('Request', lambda row: row.request),
                    ('Karma', make_karma_icon),
                    ('Age', lambda row: row.get_submitted_age()),
                ]
            ],
        }

        for key, value in grids.items():
            if not value[RESULTS].count():
                grids[key].append(None)
                continue
            if value[RESULTS].count() > 5:
                value[RESULTS] = value[RESULTS][:5]
            value[RESULTS] = list(value[RESULTS])
            grids[key].append(DataGrid(fields=value[FIELDS],
                                       default=value[RESULTS]))

        return dict(now=datetime.utcnow(), mine=grids['mine'][-1],
                    comments=grids['comments'][-1])

    @expose(template='bodhi.templates.pkgs')
    @paginate('pkgs', default_order='name', limit=20, allow_limit_override=True)
    def pkgs(self):
        pkgs = Package.select()
        return dict(pkgs=pkgs, num_pkgs=pkgs.count())

    @expose(template="bodhi.templates.login", allow_json=True)
    def login(self, forward_url=None, previous_url=None, *args, **kw):
        if not identity.current.anonymous and identity.was_login_attempted() \
           and not identity.get_identity_errors():
            if self.jsonRequest():
                return dict(user=identity.current.user)
            raise redirect(forward_url)

        forward_url=None
        previous_url= cherrypy.request.path

        if identity.was_login_attempted():
            msg=_("The credentials you supplied were not correct or "
                  "did not grant access to this resource.")
        elif identity.get_identity_errors():
            msg=_("You must provide your credentials before accessing "
                  "this resource.")
        else:
            msg=_("Please log in.")
            forward_url= cherrypy.request.headers.get("Referer", "/")

        cherrypy.response.status=403
        return dict(message=msg, previous_url=previous_url, logging_in=True,
                    original_parameters=cherrypy.request.params,
                    forward_url=forward_url)
    @expose()
    def logout(self):
        identity.current.logout()
        raise redirect('/')

    @expose(template="bodhi.templates.list", allow_json=True)
    @paginate('updates', limit=20, allow_limit_override=True)
    def list(self, release=None, bugs=None, cves=None, status=None, type=None):
        """ Return a list of updates based on given parameters """
        log.debug("list(%s, %s, %s, %s, %s)" % (release, bugs, cves, status,
                                                type))
        query = []
        if release:
            rel = Release.byName(release)
            query.append(PackageUpdate.q.releaseID == rel.id)
        if status:
            query.append(PackageUpdate.q.status == status)
        if type:
            query.append(PackageUpdate.q.type == type)

        updates = PackageUpdate.select(AND(*query))
        num_items = updates.count()

        # Filter results by Bugs and/or CVEs
        results = []
        if bugs:
            try:
                for bug in map(Bugzilla.byBz_id, map(int, bugs.split(','))):
                    map(results.append,
                        filter(lambda x: bug in x.bugs, updates))
            except SQLObjectNotFound, e:
                flash_log(e)
                if self.jsonRequest():
                    return dict(updates=[])
            updates = results
            num_items = len(updates)
        if cves:
            try:
                for cve in map(CVE.byCve_id, cves.split(',')):
                    map(results.append,
                        filter(lambda x: cve in x.cves, updates))
            except SQLObjectNotFound, e:
                flash_log(e)
                if self.jsonRequest():
                    return dict(updates=[])
            updates = results
            num_items = len(updates)

        if self.jsonRequest():
            updates = map(str, updates)

        return dict(updates=updates, num_items=num_items)

    @expose(template="bodhi.templates.list")
    @identity.require(identity.not_anonymous())
    @paginate('updates', limit=20, allow_limit_override=True)
    def mine(self):
        """ List all updates submitted by the current user """
        updates = PackageUpdate.select(
                    OR(PackageUpdate.q.submitter == util.displayname(identity),
                       PackageUpdate.q.submitter == identity.current.user_name),
                    orderBy=PackageUpdate.q.date_pushed).reversed()
        return dict(updates=updates, num_items=updates.count(),
                    title='%s\'s updates' % identity.current.user_name)

    @expose(template='bodhi.templates.show')
    def show(self, update):
        update = PackageUpdate.byTitle(update)
        update.comments.sort(lambda x, y: cmp(x.timestamp, y.timestamp))
        return dict(update=update, comment_form=self.comment_form)

    @expose(allow_json=True)
    @identity.require(identity.not_anonymous())
    def request(self, action, update):
        """
        Request that a specified action be performed on a given update.
        Action must be one of: 'testing', 'stable', 'unpush' or 'obsolete'.
        """
        log.debug("request(%s, %s)" % (action, update))
        try:
            update = PackageUpdate.byTitle(update)
        except SQLObjectNotFound:
            flash_log("Cannot find update %s for action: %s" % (update, action))
            if self.jsonRequest(): return dict()
            raise redirect('/')
        if not util.authorized_user(update, identity):
            flash_log("Unauthorized to perform action on %s" % update.title)
            if self.jsonRequest(): return dict()
            raise redirect(update.get_url())
        if action == update.status:
            flash_log("%s already %s" % (update.title, action))
            if self.jsonRequest: return dict()
            raise redirect(update.get_url())
        if action in ('testing', 'stable'):
            if update.request in ('testing', 'stable'):
                flash_log("%s has already been submitted to %s" % (update.title,
                          update.request))
                if self.jsonRequest: return dict()
                raise redirect(update.get_url())
        elif action in ('unpush', 'obsolete'):
            update.obsolete()
            if self.jsonRequest(): return dict()
            raise redirect(update.get_url())
        else:
            flash_log("Unknown request: %s" % action)
            if self.jsonRequest(): return dict()
            raise redirect(update.get_url())

        update.request = action
        flash_log("%s has been submitted for %s" % (update.title, action))
        mail.send_admin(action, update)
        if self.jsonRequest(): return dict()
        raise redirect(update.get_url())

    @expose()
    @identity.require(identity.not_anonymous())
    def revoke(self, update):
        """ Revoke a push request for a specified update """
        update = PackageUpdate.byTitle(update)
        if not util.authorized_user(update, identity):
            flash_log("Unauthorized to revoke request for %s" % update.title)
            raise redirect(update.get_url())
        flash_log("%s request revoked" % update.request.title())
        mail.send_admin('revoke', update)
        update.request = None
        raise redirect(update.get_url())

    @expose(allow_json=True)
    @identity.require(identity.not_anonymous())
    def delete(self, update):
        """ Delete a pending update """
        try:
            update = PackageUpdate.byTitle(update)
            if not util.authorized_user(update, identity):
                flash_log("Cannot delete an update you did not submit")
                if self.jsonRequest(): return dict()
                raise redirect(update.get_url())
            if not update.pushed:
                mail.send_admin('deleted', update)
                msg = "Deleted %s" % update.title
                map(lambda x: x.destroySelf(), update.comments)
                map(lambda x: x.destroySelf(), update.builds)
                update.destroySelf()
                flash_log(msg)
            else:
                flash_log("Cannot delete a pushed update")
        except SQLObjectNotFound:
            flash_log("Update %s does not exist" % update)
        if self.jsonRequest(): return dict()
        raise redirect("/")

    @identity.require(identity.not_anonymous())
    @expose(template='bodhi.templates.form')
    def edit(self, update):
        """ Edit an update """
        update = PackageUpdate.byTitle(update)
        if not util.authorized_user(update, identity):
            flash_log("Cannot edit an update you did not submit")
            raise redirect(update.get_url())
        values = {
                'builds'    : {'text':update.title, 'hidden':update.title},
                'release'   : update.release.long_name,
                'testing'   : update.status == 'testing',
                'type'      : update.type,
                'notes'     : update.notes,
                'bugs'      : update.get_bugstring(),
                'cves'      : update.get_cvestring(),
                'edited'    : update.title
        }
        return dict(form=update_form, values=values, action=url("/save"))

    @expose(allow_json=True)
    @error_handler(new.index)
    @validate(form=update_form)
    @identity.require(identity.not_anonymous())
    def save(self, builds, release, type, cves, notes, bugs, close_bugs=False,
             edited=False, **kw):
        """
        Save an update.  This includes new updates and edited.
        """
        log.debug("save(%s, %s, %s, %s, %s, %s, %s, %s, %s)" % (builds, release,
            type, cves, notes, bugs, close_bugs, edited, kw))

        note = ''
        update_builds = []
        if not cves: cves = []
        if not bugs: bugs = []
        release = Release.select(
                        OR(Release.q.long_name == release,
                           Release.q.name == release))[0]

        # Parameters used to re-populate the update form if something fails
        params = {
                'builds.text' : ' '.join(builds),
                'release'     : release.long_name,
                'type'        : type,
                'cves'        : ' '.join(cves),
                'bugs'        : ' '.join(map(str, bugs)),
                'notes'       : notes,
                'close_bugs'  : close_bugs and 'True' or '',
                'edited'      : edited
        }

        # Make sure the submitter has commit access to these builds
        for build in builds:
            nvr = util.get_nvr(build)
            people = None
            groups = None
            try:
                people, groups = get_pkg_pushers(nvr[0],
                                        release.long_name.split()[0],
                                        release.long_name[-1])
            except Exception, e:
                flash_log(e)
                if self.jsonRequest():
                    return dict()
                raise redirect('/new', **params)
            if not identity.current.user_name in people[0] and \
               not 'releng' in identity.current.groups and \
               not 'security_respons' in identity.current.groups and \
               not filter(lambda x: x in identity.current.groups, groups[0]):
                flash_log("%s does not have commit access to %s" % (
                          identity.current.user_name, nvr[0]))
                if self.jsonRequest(): return dict()
                raise redirect('/new', **params)

        # Disallow adding or removing of builds when an update is testing or
        # stable.  If we're in a pending state, we destroy them all and
        # create them later -- to allow for adding/removing of builds.
        if edited:
            update = PackageUpdate.byTitle(edited)
            if update.status in ('testing', 'stable'):
                if filter(lambda build: build not in edited, builds) or \
                   filter(lambda build: build not in builds, edited.split()):
                    flash_log("You must unpush this update before you can "
                              "add or remove any builds.")
                    if self.jsonRequest(): return dict()
                    raise redirect(update.get_url())
            map(lambda build: build.destroySelf(), update.builds)

        # Make sure the selected release matches the Koji tag for this build
        koji = buildsys.get_session()
        for build in builds:
            log.debug("Validating koji tag for %s" % build)
            tag_matches = False
            candidate = '%s-updates-candidate' % release.dist_tag
            try:
                for tag in koji.listTags(build):
                    if tag['name'] == candidate:
                        log.debug("%s built with tag %s" % (build, tag['name']))
                        tag_matches = True
                        break
            except GenericError, e:
                flash_log("Invalid build: %s" % build)
                if self.jsonRequest():
                    return dict()
                raise redirect('/new', **params)
            if not tag_matches:
                flash_log("%s build is not tagged with %s" % (build, candidate))
                if self.jsonRequest():
                    return dict()
                raise redirect('/new', **params)

            # Get the package; if it doesn't exist, create it.
            nvr = util.get_nvr(build)
            try:
                package = Package.byName(nvr[0])
            except SQLObjectNotFound:
                package = Package(name=nvr[0])

            kojiBuild = koji.getBuild(build)

            # Check for broken update paths against all previous releases
            tag = release.dist_tag
            while True:
                try:
                    for kojiTag in (tag, tag + '-updates'):
                        log.debug("Checking for broken update paths in " + tag)
                        for oldBuild in koji.listTagged(kojiTag, package=nvr[0]):
                            if rpm.labelCompare(util.build_evr(kojiBuild),
                                                util.build_evr(oldBuild)) < 0:
                                flash_log("Broken update path: %d:%s is older "
                                          "than %d:%s in %s" % (
                                          kojiBuild['epoch'], kojiBuild['nvr'],
                                          oldBuild['epoch'], oldBuild['nvr'],
                                          kojiTag))
                                raise redirect('/new', **params)
                except GenericError:
                    break

                # Check against the previous release (until one doesn't exist)
                tag = tag[:-1] + str(int(tag[-1]) - 1)

            try:
                pkgBuild = PackageBuild(nvr=build, package=package)
                update_builds.append(pkgBuild)
            except (PostgresIntegrityError, SQLiteIntegrityError,
                    DuplicateEntryError):
                flash_log("Update for %s already exists" % build)
                if self.jsonRequest():
                    return dict()
                raise redirect('/new', **params)

        # Modify or create the PackageUpdate
        if edited:
            p = PackageUpdate.byTitle(edited)
            try:
                p.set(release=release, date_modified=datetime.utcnow(),
                      notes=notes, type=type, title=','.join(builds),
                      close_bugs=close_bugs)
                log.debug("Edited update %s" % edited)
            except (DuplicateEntryError, PostgresIntegrityError,
                    SQLiteIntegrityError):
                flash_log("Update already exists for build in: %s" % 
                          ' '.join(builds))
                if self.jsonRequest():
                    return dict()
                raise redirect('/new', **params)
        else:
            try:
                p = PackageUpdate(title=','.join(builds), release=release,
                                  submitter=identity.current.user_name,
                                  notes=notes, type=type, close_bugs=close_bugs)
                log.info("Adding new update %s" % builds)
            except (PostgresIntegrityError, SQLiteIntegrityError,
                    DuplicateEntryError):
                flash_log("Update for %s already exists" % builds)
                if self.jsonRequest():
                    return dict()
                raise redirect('/new', **params)

        # Add the PackageBuilds to our PackageUpdate
        map(p.addPackageBuild, update_builds)

        # Add/remove the necessary Bugzillas and CVEs
        p.update_bugs(bugs)
        p.update_cves(cves)

        # If there are any CVEs or security bugs, make sure this update is
        # marked as security
        if p.type != 'security':
            for bug in p.bugs:
                if bug.security:
                    p.type = 'security'
                    note += '; Security bug provided, changed update type ' + \
                            'to security'
                    break
        if p.cves != [] and (p.type != 'security'):
            p.type = 'security'
            note += '; CVEs provided, changed update type to security'

        if edited:
            mail.send(p.submitter, 'edited', p)
            flash_log("Update successfully edited" + note)
        else:
            # Notify security team of newly submitted security updates
            if p.type == 'security':
                mail.send(config.get('security_team'), 'new', p)
            mail.send(p.submitter, 'new', p)
            flash_log("Update successfully created, please submit it to a "
                      "repository" + note)

        # For command line submissions, return PackageUpdate.__str__()
        if self.jsonRequest():
            return dict(update=str(p))

        raise redirect(p.get_url())

    @expose(template='bodhi.templates.list')
    @paginate('updates', limit=20, allow_limit_override=True)
    @identity.require(identity.not_anonymous())
    def default(self, *args, **kw):
        """
        This method allows for the following requests

            /release/status/update
            /release/security
            /release/update_id
            /packagename
        """
        args = list(args)
        status = 'stable'
        order = PackageUpdate.q.date_pushed
        template = 'bodhi.templates.list'
        release = None
        single = False
        query = []

        # /packagename
        if len(args) == 1:
            try:
                package = Package.byName(args[0])
                return dict(tg_template='bodhi.templates.pkg', pkg=package,
                            updates=[])
            except SQLObjectNotFound:
                pass

        # /release
        try:
            release = Release.byName(args[0])
            query.append(PackageUpdate.q.releaseID == release.id)
            del args[0]
        except SQLObjectNotFound:
            pass

        # /release/{update_id,status}
        if len(args):
            if args[0] in ('testing', 'stable', 'pending', 'obsolete'):
                if args[0] == 'testing':
                    template = 'bodhi.templates.testing'
                elif args[0] == 'pending':
                    template = 'bodhi.templates.pending'
                    order = PackageUpdate.q.date_submitted
                status = args[0]
                query.append(PackageUpdate.q.status == status)
            elif args[0] == 'security':
                query.append(PackageUpdate.q.type == 'security')
                query.append(PackageUpdate.q.pushed == True)
                query.append(PackageUpdate.q.status == status)
                status = 'security'
            else:
                query.append(PackageUpdate.q.update_id == args[0])
                single = True
            del args[0]
        else:
            query.append(PackageUpdate.q.status == status)

        # /release/status/update
        if len(args):
            query.append(PackageUpdate.q.title == args[0])
            del args[0]

        # Run the query that we just built
        updates = PackageUpdate.select(AND(*query), orderBy=order).reversed()

        num_updates = updates.count()
        if num_updates and (num_updates == 1 or single):
            update = updates[0]
            update.comments.sort(lambda x, y: cmp(x.timestamp, y.timestamp))
            return dict(tg_template='bodhi.templates.show', update=update,
                        updates=[], comment_form=self.comment_form,
                        values={'title' : update.title})
        elif num_updates > 1:
            try:
                return dict(tg_template=template, updates=updates,
                            num_items=num_updates, title='%s %s Updates' % (
                            release.long_name, status.title()))
            except AttributeError:
                pass
        else:
            return dict(tg_template=template, updates=[], num_items=0,
                        title='No updates found')

        flash_log("The path %s cannot be found" % cherrypy.request.path)
        raise redirect("/")

    @expose()
    @error_handler()
    @validate(form=comment_form)
    @validate(validators={ 'karma' : validators.Int() })
    @identity.require(identity.not_anonymous())
    def comment(self, text, title, karma, tg_errors=None):
        update = PackageUpdate.byTitle(title)
        if tg_errors:
            flash_log(tg_errors)
        else:
            update.comment(text, karma)
        raise redirect(update.get_url())

    @expose(template='bodhi.templates.comments')
    @paginate('comments', limit=20, allow_limit_override=True)
    def comments(self):
        data = Comment.select(Comment.q.author != 'bodhi',
                              orderBy=Comment.q.timestamp).reversed()
        return dict(comments=data, num_items=data.count())

    @expose(template='bodhi.templates.confirmation')
    @identity.require(identity.not_anonymous())
    def confirm_delete(self, nvr=None, ok=None, cancel=None):
        update = PackageUpdate.byTitle(nvr)
        if ok:
            flash(_(u"Delete completed"))
            raise redirect('/delete/%s' % update.title)
        if cancel:
            flash(_(u"Delete canceled" ))
            raise redirect(update.get_url())
        return dict(form=self.ok_cancel_form, nvr=nvr)

    @expose(template='bodhi.templates.obsolete')
    def foobar(self):
        from bodhi.widgets import ObsoleteForm
        return dict(dialog=ObsoleteForm('kernel'))

    @expose("json")
    def obsolete(self, updates, *args, **kw):
        """
        Called by our ObsoleteForm widget.  This method will
        request that any specified updates be marked as obsolete
        """
        log.debug("obsolete(%s, %s, %s)" % (updates, args, kw))
        errors = []
        if type(updates) != list:
            updates = [updates]
        for update in updates:
            up = PackageBuild.byNvr(update).updates[0]
            if not util.authorized_user(up, identity):
                msg = "Unauthorized to obsolete %s" % up.title
                errors.append(msg)
                flash_log(msg)
            else:
                update.obsolete()
        return len(errors) and errors[0] or "Done!"
