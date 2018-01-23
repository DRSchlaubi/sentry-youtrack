# -*- encoding: utf-8 -*-
import json

from django import forms
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse
from django.utils.translation import ugettext_lazy as _
from sentry.models import GroupMeta
from sentry.plugins.bases.issue import IssuePlugin
from sentry.exceptions import PluginError

from . import VERSION
from .forms import (NewIssueForm, AssignIssueForm, DefaultFieldForm,
                    YouTrackProjectForm, VERIFY_SSL_CERTIFICATE)
from .utils import cache_this, get_int
from .youtrack import YouTrackClient
from requests.exceptions import ConnectionError, HTTPError, SSLError


class YouTrackPlugin(IssuePlugin):
    author = u"Adam Bogdał"
    author_url = "https://github.com/getsentry/sentry-youtrack/"
    version = VERSION
    slug = "youtrack"
    title = _("YouTrack")
    conf_title = title
    conf_key = slug
    new_issue_form = NewIssueForm
    assign_issue_form = AssignIssueForm
    create_issue_template = "sentry_youtrack/create_issue_form.html"
    assign_issue_template = "sentry_youtrack/assign_issue_form.html"
    project_conf_template = "sentry_youtrack/project_conf_form.html"
    project_fields_form = YouTrackProjectForm
    default_fields_key = 'default_fields'

    resource_links = [
        (_("Bug Tracker"), "https://github.com/getsentry/sentry-youtrack/issues/"),
        (_("Source"), "https://github.com/getsentry/sentry-youtrack/")]

    def is_configured(self, request, project, **kwargs):
        return bool(self.get_option('project', project))

    def get_youtrack_client(self, project):
        settings = {
            'url': self.get_option('url', project),
            'username': self.get_option('username', project),
            'password': self.get_option('password', project),
            'verify_ssl_certificate': VERIFY_SSL_CERTIFICATE}
        return YouTrackClient(**settings)

    def get_project_fields(self, project):
        @cache_this(600)
        def cached_fields(ignore_fields):
            yt_client = self.get_youtrack_client(project)
            return list(yt_client.get_project_fields(
                self.get_option('project', project), ignore_fields))
        return cached_fields(self.get_option('ignore_fields', project))

    def get_initial_form_data(self, request, group, event, **kwargs):
        initial = {
            'title': self._get_group_title(request, group, event),
            'description': self._get_group_description(request, group, event),
            'tags': self.get_option('default_tags', group.project),
            'default_fields': self.get_option(
                self.default_fields_key, group.project)}
        return initial

    def get_new_issue_title(self):
        return _("Create YouTrack Issue")

    def get_existing_issue_title(self):
        return _("Assign existing YouTrack issue")

    def get_new_issue_form(self, request, group, event, **kwargs):
        if request.POST or request.GET.get('form'):
            return self.new_issue_form(
                project_fields=self.get_project_fields(group.project),
                data=request.POST or None,
                initial=self.get_initial_form_data(request, group, event))
        return forms.Form()

    def create_issue(self, request, group, form_data, **kwargs):
        project_fields = self.get_project_fields(group.project)
        project_form = self.project_fields_form(project_fields, request.POST)
        project_field_values = project_form.get_project_field_values()

        tags = filter(None, map(lambda x: x.strip(),
                                form_data['tags'].split(',')))
        yt_client = self.get_youtrack_client(group.project)

        issue_data = {
            'project': self.get_option('project', group.project),
            'summary': form_data.get('title'),
            'description': form_data.get('description')}
        issue_id = yt_client.create_issue(issue_data)

        for field, value in project_field_values.iteritems():
            if value:
                value = [value] if type(value) != list else value
                cmd = map(lambda x: "%s %s" % (field, x), value)
                yt_client.execute_command(issue_id, " ".join(cmd))
        if tags:
            yt_client.add_tags(issue_id, tags)
        return issue_id

    def get_issue_url(self, group, issue_id, **kwargs):
        url = self.get_option('url', group.project).rstrip('/')
        return "%s/issue/%s" % (url, issue_id)

    def get_view_response(self, request, group):
        if request.is_ajax() and request.GET.get('action'):
            return self.view(request, group)
        return super(YouTrackPlugin, self).get_view_response(request, group)

    def actions(self, request, group, action_list, **kwargs):
        action_list = (super(YouTrackPlugin, self)
                       .actions(request, group, action_list, **kwargs))
        prefix = self.get_conf_key()
        if self.is_configured(request, group.project):
            if not GroupMeta.objects.get_value(group, '%s:tid' % prefix, None):
                url = self.get_url(group) + "?action=assign_issue"
                action_list.append((self.get_existing_issue_title(), url))
        return action_list

    def view(self, request, group, **kwargs):
        def get_action_view():
            action_view = "%s_view" % request.GET.get('action')
            if request.GET.get('action') and hasattr(self, action_view):
                return getattr(self, action_view)
        view = get_action_view() or super(YouTrackPlugin, self).view
        return view(request, group, **kwargs)

    def assign_issue_view(self, request, group):
        form = self.assign_issue_form(request.POST or None)
        if form.is_valid():
            issue_id = form.cleaned_data['issue']
            prefix = self.get_conf_key()
            GroupMeta.objects.set_value(group, '%s:tid' % prefix, issue_id)
            return self.redirect(group.get_absolute_url())
        context = {
            'form': form,
            'title': self.get_existing_issue_title()}
        return self.render(self.assign_issue_template, context)

    def project_issues_view(self, request, group):
        query = request.POST.get('q', None)
        page = get_int(request.POST.get('page'), 1)
        page_limit = get_int(request.POST.get('page_limit'), 15)
        offset = (page-1) * page_limit

        yt_client = self.get_youtrack_client(group.project)
        project_id = self.get_option('project', group.project)
        project_issues = yt_client.get_project_issues(
            project_id, offset=offset, limit=page_limit + 1, query=query)

        data = {
            'more': len(project_issues) > page_limit,
            'issues': project_issues[:page_limit]}
        return HttpResponse(json.dumps(data, cls=DjangoJSONEncoder))

    def save_field_as_default_view(self, request, group):
        form = DefaultFieldForm(self, group.project, request.POST or None)
        if form.is_valid():
            form.save()
        return HttpResponse()

    def has_project_conf(self):
        return True

    def get_config(self, project, user, **kwargs):
        config = []
       
        initial = {
            'project': self.get_option('project', project),
            'url': self.get_option('url', project),
            'username': self.get_option('username', project),
            'password': self.get_option('password', project),
        }
        # filtering out null values
        initial = dict((k, v) for k, v in initial.iteritems() if v)

        self.config_form = YouTrackConfiguration(initial)
        return self.config_form.config

    def validate_config(self, project, config, actor):
        super(YouTrackPlugin, self).validate_config(project, config, actor)
        errors = self.config_form.client_errors
        for error, message in errors.iteritems():
            # raise PluginError(message)
            pass
        return config

class YouTrackConfiguration(object):

    error_message = {
        'client': _("Unable to connect to YouTrack."),
        'project_unknown': _('Unable to fetch project'),
        'project_not_found': _('Project not found: %s'),
        'invalid_ssl': _("SSL certificate  verification failed."),
        'invalid_password': _('Invalid username or password.'),
        'invalid_project': _('Invalid project: \'%s\''),
        'missing_fields': _('Missing required fields.'),
        'perms': _("User doesn't have Low-level Administration permissions."),
        'required': _("This field is required.")}

    def __init__(self, initial):
        self.config = self.build_default_fields()
        self.set_initial(initial)

    def set_initial(self, initial):
        self.client_errors = {}
        if self.has_client_fields(initial):
            client = self.get_youtrack_client(initial)
            if client:
                if initial.get('project'):
                    choices = self.get_ignore_field_choices(
                        client, initial.get('project'))
                    self.config.append({
                        'name':'ignore_fields',
                        'label':'Ignore Fields',
                        'type':'select',
                        'choices':choices,
                        'required':False,
                        'help': 'These fields will not appear on the form.',})

                choices = self.get_project_field_choices(client, initial.get('project'))
                self.config.append({
                    'name':'project',
                    'label':'Linked Project',
                    'type':'select',
                    'choices': choices,
                    'required':True,})
                
                self.config.append({'name':'default_tags',
                'label':'Default Tags',
                'type':'text',
                'required':False,
                'placeholder': 'e.g. sentry',
                'help': 'Comma-separated list of tags.',})

                if not initial.get('project'):
                    self.second_step_msg = _(
                        "Your credentials are valid but plugin is NOT active "
                        "yet. Please fill in remaining required fields.")

    def has_client_fields(self, initial):
        return initial.get('password') and initial.get('username') and initial.get('url')
                
    def build_default_fields(self):
        url = {'name':'url',
                'label':'YouTrack Instance URL',
                'type':'text',
                'required':True,
                'placeholder': 'e.g. "https://yoursitename.myjetbrains.com/youtrack/"',}
        username = {'name':'username',
                'label':'Username',
                'type':'text',
                'required':True,
                'help': 'User should have admin rights.',}
        password = {'name':'password',
                'label':'Password',
                'type':'secret',
                'required':False,
                'help': 'Only enter a password if you want to change it.',}
       
        return [url, username, password]
        

    def get_youtrack_client(self, data, additional_params=None):
        yt_settings = {
            'url': data.get('url'),
            'username': data.get('username'),
            'password': data.get('password'),
            'verify_ssl_certificate': VERIFY_SSL_CERTIFICATE}
        if additional_params:
            yt_settings.update(additional_params)

        client = None
        try:
            client = YouTrackClient(**yt_settings)
        except (HTTPError, ConnectionError) as e:
            if e.response is not None and e.response.status_code == 403:
                self.client_errors['username'] = self.error_message[
                    'invalid_password']
            else:
                self.client_errors['url'] = self.error_message['client']
        except (SSLError, TypeError) as e:
            self.client_errors['url'] = self.error_message['invalid_ssl']
        if client:
            try:
                client.get_user(yt_settings.get('username'))
            except HTTPError as e:
                if e.response.status_code == 403:
                    self.client_errors['username'] = self.error_message['perms']
                    client = None
        return client

    def get_ignore_field_choices(self, client, project):
        try:
            fields = list(client.get_project_fields_list(project))
        except HTTPError:
            self.client_errors['project'] = self.error_message[
                'invalid_project'] % (project,)
        else:
            names = [field['name'] for field in fields]
            return zip(names, names)
        return []

    def get_project_field_choices(self, client, project):
        choices = [(' ', u"- Choose project -")]
        try:
            projects = list(client.get_projects())
        except HTTPError:
            self.client_errors['project'] = self.error_message[
                'invalid_project'] % (project, )
        else:
            for project in projects:
                display = "%s (%s)" % (project['name'], project['id'])
                choices.append((project['id'], display))
        return choices
    
    def get_project_fields_list(self, client, project_id):
        try:
            return list(client.get_project_fields_list(project_id))
        except (HTTPError, ConnectionError) as e:
            if e.response is not None and e.response.status_code == 404:
                self.client_errors['project'] = self.error_message['project_not_found'] % project_id
            else:
                self.client_errors['project'] = self.error_message['project_unknown']

    def get_projects(self, client):
        try:
            return list(client.get_projects())
        except (HTTPError, ConnectionError) as e:
            if e.response is not None and e.response.status_code == 404:
                self.client_errors['project'] = self.error_message['project_not_found'] % project_id
            else:
                self.client_errors['project'] = self.error_message['project_unknown']