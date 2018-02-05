# Remove this once all the sys.exit(1) is removed
from __future__ import print_function
import os
import sys
import helm
from . import template


# This could be defined in App flag _apply method but
# it would be harder to patch for tests
def _update_compilation_vm_disk_size(manifest):
    package_file = manifest.get('path')
    if not isinstance(package_file, basestring) or not os.path.exists(package_file):
        print('Package file "{}" not found! Please check the manifest path in your tile.yml file.'.format(package_file), file=sys.stderr)
        sys.exit(1)
    package_size = os.path.getsize(package_file) // (1024 * 1024) # bytes to megabytes
    return package_size


class FlagBase(object):
    @classmethod
    def generate_release(self, config_obj, package):
        release_name = config_obj['name']
        default_release = {
            'name': release_name,
            'packages': [],
            'jobs': [] }
        release = config_obj.get('releases', {}).get(release_name, default_release)
        if not package in release.get('packages', []):
            release['packages'] = release.get('packages', []) + [ package ]

        if not config_obj.get('releases'):
            config_obj['releases'] = dict()
        config_obj['releases'][release_name] = release
        self._apply(config_obj, package, release)


class BoshRelease(FlagBase):
    @classmethod
    def _apply(self, config_obj, package, release):
        # TODO: Remove the dependency on this in templates
        package['is_bosh_release'] = True

        packagename = package['name']
        properties = {'name': packagename}
        for job in package.get('jobs', []):
            jobname = job['name']
            job['is_static'] = job.get('static_ip', 0) > 0
            if job.get('static_ip', 0) > 0:
                properties.update({
                    job['varname']: {
                        'host': '(( .{}.first_ip ))'.format(jobname),
                        'hosts': '(( .{}.ips ))'.format(jobname),
                    },
                })
        package['properties'] = {packagename: properties}

    @classmethod
    def generate_release(self, config_obj, package):
        release_name = package['name']
        release = package
        if config_obj.get('releases', {}).get(release_name):
            print('duplicate bosh release', release_name, 'in configuration', file=sys.stderr)
            sys.exit(1)
        config_obj['releases'][release_name] = release
        self._apply(config_obj, package, release)


class Cf(FlagBase):
    @classmethod
    def _apply(self, config_obj, package, release):
        # TODO: Remove the dependency on this in templates
        package['is_cf'] = True
        release['is_cf'] = True

        if 'deploy-all' not in [job['name'] for job in release['jobs']]:
            release['jobs'] += [{
                'name': 'deploy-all',
                'type': 'deploy-all',
                'lifecycle': 'errand',
                'post_deploy': True,
		'packages': [{'name': 'cf_cli'}],
            }]
        if 'delete-all' not in [job['name'] for job in release['jobs']]:
            release['jobs'] += [{
                'name': 'delete-all',
                'type': 'delete-all',
                'lifecycle': 'errand',
                'pre_delete': True,
		'packages': [{'name': 'cf_cli'}],
            }]
        if { 'name': 'deploy-all' } not in config_obj.get('post_deploy_errands', []):
            config_obj['post_deploy_errands'] = config_obj.get('post_deploy_errands', []) + [{ 'name': 'deploy-all' }]
        if { 'name': 'delete-all' } not in config_obj.get('pre_delete_errands', []):
            config_obj['pre_delete_errands'] = config_obj.get('pre_delete_errands', []) + [{ 'name': 'delete-all' }]
        if not 'cf_cli' in [p['name'] for p in release['packages']]:
            release['packages'] += [{
                'name': 'cf_cli',
                'files': [{
                    'name': 'cf-linux-amd64.tgz',
                    'path': 'http://cli.run.pivotal.io/stable?release=linux64-binary&source=github-rel'
                },{
                    'name': 'all_open.json',
                    'path': template.path('src/templates/all_open.json')
                }],
                'template': 'cf_cli',
                'dir': 'blobs'
            }]
        config_obj.tile_metadata['requires_product_versions'] = config_obj.get('requires_product_versions', []) + [
            {
                'name': 'cf',
                'version': '>= 1.9'
            }
        ]


class DockerBosh(FlagBase):
    @classmethod
    def _apply(self, config_obj, package, release):
        # TODO: Remove the dependency on this in templates
        package['is_docker_bosh'] = True

        config_obj['requires_docker_bosh'] = True

        release['requires_docker_bosh'] = True
        requires_docker_bosh = True
        release['packages'] += [{
            'name': 'common',
            'files': [{
                'name': 'utils.sh',
                'path': template.path('src/common/utils.sh')
            }],
            'template': 'common',
            'dir': 'src'
        }]

        release['jobs'] += [{
            'name': 'docker-bosh-' + package['name'],
            'template': 'docker-bosh',
            'package': package
        }]
        if requires_docker_bosh:
            version = None
            version_param = '?v=' + version if version else ''
            config_obj['releases']['docker-boshrelease'] = {
                'name': 'docker-boshrelease',
                'path': 'https://bosh.io/d/github.com/cf-platform-eng/docker-boshrelease' + version_param,
            }
            config_obj['releases']['routing'] = {
                'name': 'routing',
                'path': 'https://bosh.io/d/github.com/cloudfoundry-incubator/cf-routing-release',
            }

        packagename = package['name']
        properties = package.get('properties', {packagename: {}})
        properties[packagename].update(
            {'name': packagename,
            'host': '(( .docker-bosh-{}.first_ip ))'.format(packagename),
            'hosts': '(( .docker-bosh-{}.ips ))'.format(packagename),
        })
        package['properties'] = properties
        for container in package['manifest']['containers']:
            envfile = container.get('env_file', [])
            envfile.append('/var/vcap/jobs/docker-bosh-{}/bin/opsmgr.env'.format(package['name']))
            container['env_file'] = envfile


class Decorator(FlagBase):
    @classmethod
    def _apply(self, config_obj, package, release):
        release['requires_meta_buildpack'] = True
        config_obj['releases']['meta-buildpack'] = {
            'name': 'meta-buildpack',
            'path': 'github://cf-platform-eng/meta-buildpack/meta-buildpack.tgz',
            'jobs': [
                {
                    'name': 'deploy-meta-buildpack',
                    'type': 'deploy-all',
                    'lifecycle': 'errand',
                    'post_deploy': True
                },
                {
                    'name': 'delete-meta-buildpack',
                    'type': 'delete-all',
                    'lifecycle': 'errand',
                    'pre_delete': True
                }
            ]
        }

class App(FlagBase):
    @classmethod
    def _apply(self, config_obj, package, release):
        # TODO: Remove the dependency on this in templates
        package['is_app'] = True

        for link_name, link in package.get('consumes', {}).iteritems():
            release['consumes'] = release.get('consumes', {})
            release['consumes'][link_name] = link
            release['consumes_for_deployment'] = release.get('consumes_for_deployment', {})
            release['consumes_for_deployment'][link_name] = link.copy()
            release['consumes_for_deployment'][link_name].pop('type',None)
            release['consumes_for_deployment'][link_name].pop('optional',None)
        manifest = package.get('manifest', { 'name': package['name'] })
        package['app_manifest'] = manifest
        if manifest.get('path'):
            config_obj['compilation_vm_disk_size'] = max(
                config_obj['compilation_vm_disk_size'],
                4 * _update_compilation_vm_disk_size(manifest))

        packagename = package['name']
        properties = package.get('properties', {packagename: {}})
        properties[packagename].update(
            {'name': packagename,
            'app_manifest': package['app_manifest'],
            'auto_services': package.get('auto_services', []),
        })
        package['properties'] = properties


class ExternalBroker(FlagBase):
    @classmethod
    def _apply(self, config_obj, package, release):
        # TODO: Remove the dependency on this in templates
        package['is_external_broker'] = True

        packagename = package['name']
        properties = package.get('properties', {packagename: {}})
        properties[packagename].update(
            {'name': packagename,
            'url': '(( .properties.{}_url.value ))'.format(packagename),
            'user': '(( .properties.{}_user.value ))'.format(packagename),
            'password': '(( .properties.{}_password.value ))'.format(packagename),
        })
        package['properties'] = properties


class Broker(FlagBase):
    @classmethod
    def _apply(self, config_obj, package, release):
        # TODO: Remove the dependency on this in templates
        package['is_broker'] = True
        packagename = package['name']
        properties = package.get('properties', {packagename: {}})
        properties[packagename].update(
            {'name': packagename,
            'enable_global_access_to_plans': '(( .properties.{}_enable_global_access_to_plans.value ))'.format(packagename),
        })
        package['properties'] = properties


class Buildpack(FlagBase):
    @classmethod
    def _apply(self, config_obj, package, release):
        # TODO: Remove the dependency on this in templates
        package['is_buildpack'] = True

        packagename = package['name']
        properties = package.get('properties', {packagename: {}})
        properties[packagename].update(
            {'name': packagename,
            'buildpack_order': '(( .properties.{}_buildpack_order.value ))'.format(packagename),
        })
        package['properties'] = properties

class Helm(FlagBase):
    @classmethod
    def _apply(self, config_obj, package, release):
        # Read the helm chart and bundle all required docker images
        chart_info = helm.get_chart_info(package['path'])
        for image in chart_info['required_images']:
            image_name = image.split('/')[-1]
            image_package_name = package['name'] + '-images'
            image_packages = [p for p in release['packages'] if p['name'] == image_package_name]
            if image_packages:
                image_package = image_packages[0]
            else:
                image_package = {
                    'name': image_package_name,
                    'files': [],
                    'dir': 'blobs'
                }
                release['packages'] += [ image_package ]
            image_package['files'] += [{
                'name': image_name,
                'path': 'docker:' + image
            }]
        # Add errands if they are not already here
        if 'deploy-charts' not in [job['name'] for job in release['jobs']]:
            deploy_charts_job = {
                'name': 'deploy-charts',
                'type': 'deploy-charts',
                'lifecycle': 'errand',
                'post_deploy': True
            }
            if image_package is not None:
                deploy_charts_job['packages'] = deploy_charts_job.get('packages',[])
                deploy_charts_job['packages'] += [ image_package ]
            release['jobs'] += [ deploy_charts_job ]
        if 'delete-charts' not in [job['name'] for job in release['jobs']]:
            release['jobs'] += [{
                'name': 'delete-charts',
                'type': 'delete-charts',
                'lifecycle': 'errand',
                'pre_delete': True
            }]
        if { 'name': 'deploy-charts' } not in config_obj.get('post_deploy_errands', []):
            config_obj['post_deploy_errands'] = config_obj.get('post_deploy_errands', []) + [{ 'name': 'deploy-charts' }]
        if { 'name': 'delete-charts' } not in config_obj.get('pre_delete_errands', []):
            config_obj['pre_delete_errands'] = config_obj.get('pre_delete_errands', []) + [{ 'name': 'delete-charts' }]
        if not 'helm_cli' in [p['name'] for p in release['packages']]:
            latest_helm_tag = helm.get_latest_release_tag()
            release['packages'] += [{
                'name': 'helm_cli',
                'files': [{
                    'name': 'helm-linux-amd64.tar.gz',
                    'path': 'https://kubernetes-helm.storage.googleapis.com/helm-{}-linux-amd64.tar.gz'.format(latest_helm_tag),
                }],
                'template': 'helm_cli',
                'dir': 'blobs'
            }]
        if False:
            # Turning this off for now so that the tile can be installed in foundations that don't have PKS for testing
            config_obj.tile_metadata['requires_product_versions'] = config_obj.get('requires_product_versions', []) + [
                {
                    'name': 'pivotal-container-service',
                    'version': '>= 0.8'
                }
            ]
