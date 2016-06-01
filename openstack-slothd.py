#!/usr/bin/env python

##
## A simple Ceilometer script that runs within a Cloudlab OpenStack
## experiment and writes several resource utilization files into
## /root/openstack-slothd .
##
## This script runs every N minutes (default 10), and reports its
## metrics over 5 time periods: last 10 minutes, hour, 6 hours, day,
## week.  For each period, for each physical host in the experiment, it
## reports the number of distinct VMs that existed, CPU utilization for
## each VM, and network traffic for each VM.  
##

import os
import time
import sys
import hashlib
import logging
import traceback
import pprint
import json
import shutil
from ceilometerclient import client
from ceilometerclient.v2.query import QueryManager

VERSION = 1

CLOUDLAB_AUTH_FILE = '/root/setup/admin-openrc.py'
KEYSTONE_OPTS = [ 'OS_PROJECT_DOMAIN_ID','OS_USER_DOMAIN_ID',
                  'OS_PROJECT_NAME','OS_TENANT_NAME',
                  'OS_USERNAME','OS_PASSWORD','OS_AUTH_URL' ]
#'OS_IDENTITY_API_VERSION'

#
# We often want to see "everything", and ceilometer limits us by
# default, so assume "everything" falls into UINT32_MAX.  What a mess.
#
LIMIT = 0xffffffff
MINUTE = 60
HOUR = MINUTE * 60
DAY = HOUR * 24
WEEK = DAY * 7

PERIODS = [10*MINUTE,HOUR,6*HOUR,DAY,WEEK]

OURDIR = '/root/setup'
OUTDIR = '/root/setup'
OUTBASENAME = 'cloudlab-openstack-stats.json'
OURDOMAIN = None

projects = {}
resources = {}
vhostnames = {}
phostnames = {}
r_hostnames = {}

LOG = logging.getLogger(__name__)
# Define a default handler at INFO logging level
logging.basicConfig(level=logging.DEBUG)
    
pp = pprint.PrettyPrinter(indent=2)

def build_keystone_args():
    global KEYSTONE_OPTS, CLOUDLAB_AUTH_FILE
    
    ret = dict()
    # First, see if they're in the env:
    for opt in KEYSTONE_OPTS:
        if opt in os.environ:
            ret[opt.lower()] = os.environ[opt]
        pass
    # Second, see if they're in a special Cloudlab file:
    if os.geteuid() == 0 and os.path.exists(CLOUDLAB_AUTH_FILE):
        try:
            f = open(CLOUDLAB_AUTH_FILE,'r')
            while True:
                line = f.readline()
                if not line:
                    break
                line = line.rstrip('\n')
                vva = line.split('=')
                if not vva or len(vva) != 2:
                    continue
                
                ret[vva[0].lower()] = eval(vva[1])

                pass
            f.close()
        except:
            LOG.exception("could not build keystone args!")
        pass
    elif os.geteuid() != 0:
        LOG.info("you are not root (%d); not checking %s",os.geteuid(),CLOUDLAB_AUTH_FILE)
    elif not os.path.exists(CLOUDLAB_AUTH_FILE):
        LOG.info("%s does not exist; not loading auth opts from it",CLOUDLAB_AUTH_FILE)

    return ret

def get_resource(client,resource_id):
    global resources,projects
    
    r = None
    if not resource_id in resources:
        r = client.resources.get(resource_id)
        resources[resource_id] = r
    else:
        r = resources[resource_id]
        pass
    
    return r

def get_hypervisor_hostname(client,resource):
    global resources,projects,r_hostnames
    
    #
    # This is yucky.  I have seen two different cases: one where the
    # resource.metadata.host field is a hash of the project_id and
    # hypervisor hostname -- and a second one (after a reboot) where the
    # 'host' field looks like 'compute.<HYPERVISOR-FQDN>' and there is a
    # 'node' field that has the hypervisor FQDN.  So we try to place
    # nice for both cases... use the 'node' field if it exists --
    # otherwise assume that the 'host' field has a hash.  Ugh!
    #
    # Ok, I see how this works.  If you call client.resources.list(),
    # you are shown a hash for the 'host' field.  And if you call
    # client.resources.get(resource_id) (presumably as admin, like we
    # do), you get more info.  Now, why can't they just do the same for
    # client.resources.list()?!  Anyway, we just choose not to
    # pre-initialize the resources list above, at startup, and pull them
    # all on-demand.
    #
    # Well, that was a nice theory, but it doesn't seem deterministic.  I
    # wonder if there's some kind of race.  Anyway, have to leave all this
    # hash crap in here for now.
    #
    if 'node' in resource.metadata \
      and resource.metadata['node'].endswith(OURDOMAIN):
        hostname = resource.metadata['node']
    elif 'host' in resource.metadata \
      and resource.metadata['host'].startswith('compute.') \
      and resource.metadata['host'].endswith(OURDOMAIN):
        hostname = resource.metadata['host'].lstrip('compute.')
    else:
        if not resource.project_id in projects:
            projects[resource.project_id] = resource.project_id
            for hostname in vhostnames.keys():
                shash = hashlib.sha224(resource.project_id + hostname)
                hh = shash.hexdigest()
                r_hostnames[hh] = hostname
                pass
            pass
        #LOG.debug("resource: " + pp.pformat(resource))
        hh = None
        try:
            hh = resource.metadata['host']
        except:
            if 'instance_id' in resource.metadata:
                LOG.info("no hostname info for resource %s; trying instance_id" % (str(resource),))
                return get_hypervisor_hostname(client,get_resource(client,resource.metadata['instance_id']))
            else:
                LOG.exception("no 'host' field in metadata for resource %s" % (str(resource,)))
            pass
        if not hh in r_hostnames.keys():
            LOG.error("hostname hash %s doesn't map to a known hypervisor hostname!" % (hh,))
            return None
        hostname = r_hostnames[hh]
        pass
    return hostname

def get_api_hostname(client,resource):
    if 'host' in resource.metadata:
        if resource.metadata['host'].startswith('compute.') \
          and resource.metadata['host'].endswith(OURDOMAIN):
            return resource.metadata['host'].lstrip('compute.')
        elif resource.metadata['host'].startswith('network.') \
          and resource.metadata['host'].endswith(OURDOMAIN):
            return resource.metadata['host'].lstrip('network.')
        pass
    return None

def fetchall(client):
    tt = time.gmtime()
    ct = time.mktime(tt)
    cts = time.strftime('%Y-%m-%dT%H:%M:%S',tt)

    periods = {}
    info = {}
    #datadict = {}
    
    #
    # Ok, collect all the statistics, grouped by VM, for the period.  We
    # have to specify this duration
    #
    #qm = QueryManager()
    q = client.query_samples.query(limit=1)
    for period in PERIODS:
        periods[period] = {}
        vm_dict = dict() #count=vm_0,list=[])
        cpu_util_dict = dict()
        
        daylightfactor = 0
        if time.daylight:
            daylightfactor -= HOUR
            pass
        pct = ct - period + daylightfactor
        ptt = time.localtime(pct)
        pcts = time.strftime('%Y-%m-%dT%H:%M:%S',ptt)
        q = [{'field':'timestamp','value':pcts,'op':'ge',},
             {'field':'timestamp','value':cts,'op':'lt',}]

        # First, query some rate meters for avg stats:
        meters = ['cpu_util','network.incoming.bytes.rate',
                  'network.outgoing.bytes.rate']
        for meter in meters:
            mdict = {}
            statistics = client.statistics.list(meter,#period=period,
                                                groupby='resource_id',
                                                q=q)
            LOG.debug("Statistics for %s during period %d (len %d): %s"
                      % (meter,period,len(statistics),pp.pformat(statistics)))
            for stat in statistics:
                rid = stat.groupby['resource_id']
                resource = get_resource(client,rid)
                # For whatever reason, the resource_id for the network.*
                # meters prefixes the VM UUIDs with instance-%d- ...
                # so strip that out.
                vmrid = rid
                if rid.startswith('instance-'):
                    vmrid = rid.lstrip('instance-')
                    vidx = vmrid.find('-')
                    vmrid = vmrid[(vidx+1):]
                    pass
                # Then, for the network.* meters, the results are
                # per-interface, so strip that off too so we can
                # report one number per VM.
                vidx = vmrid.find('-tap')
                if vidx > -1:
                    vmrid = vmrid[:vidx]
                    pass

                hostname = get_hypervisor_hostname(client,resource)
                LOG.info("%s for %s on %s = %f (resource=%s)"
                         % (meter,rid,hostname,stat.avg,pp.pformat(resource)))
                if not hostname in vm_dict:
                    vm_dict[hostname] = {}
                    pass
                if not vmrid in vm_dict[hostname] and 'display_name' in resource.metadata \
                   and 'image.name' in resource.metadata and 'status' in resource.metadata:
                    vm_dict[hostname][vmrid] = dict(name=resource.metadata['display_name'],
                                                    image=resource.metadata['image.name'],
                                                    status=resource.metadata['status'])
                    pass
                if not hostname in mdict:
                    mdict[hostname] = dict(total=0.0,vms={})
                    pass
                mdict[hostname]['total'] += stat.avg
                if not vmrid in mdict[hostname]['vms']:
                    mdict[hostname]['vms'][vmrid] = stat.avg
                else:
                    mdict[hostname]['vms'][vmrid] += stat.avg
                    pass
                pass
            periods[period][meter] = mdict
            pass
        
        info['vms'] = vm_dict

        # Now also query the API delta meters:
        # NB: very important that the .delete meters come first, for
        # each resource type.  Why?  Because we only put the resource
        # details into the info dict one time (because we don't know
        # how to merge details for a given resource if we see it again
        # later and it differs) -- and sometimes we know if a resource
        # is deleted based on if the delete method has been called for
        # it (i.e. for network resources); for other resources like
        # images, there's a deleted bit in the metadata we can just read.
        meters = [ 'network.delete','network.create','network.update',
                   'subnet.delete','subnet.create','subnet.update',
                   'port.delete','port.create','port.update',
                   'router.delete','router.create','router.update',
                   'image.upload','image.update' ]
        rdicts = dict()
        for meter in meters:
            idx = meter.find('.')
            if idx > -1:
                rplural = "%s%s" % (meter[0:idx],'s')
            else:
                rplural = None
                pass
            if rplural and not rplural in rdicts:
                rdicts[rplural] = dict()
                pass
            mdict = {}
            statistics = client.statistics.list(meter,#period=period,
                                                groupby='resource_id',
                                                q=q)
            LOG.debug("Statistics for %s during period %d (len %d): %s"
                      % (meter,period,len(statistics),pp.pformat(statistics)))
            for stat in statistics:
                rid = stat.groupby['resource_id']
                resource = get_resource(client,rid)
                hostname = get_api_hostname(client,resource)
                LOG.info("%s for %s on %s = %f (%s)"
                         % (meter,rid,hostname,stat.sum,pp.pformat(resource)))
                if rplural and not rid in rdicts[rplural]:
                    deleted = False
                    if meter.endswith('.delete') \
                       or ('deleted' in resource.metadata \
                           and resource.metadata['deleted'] \
                                 in ['True','true',True]):
                        deleted = True
                        pass
                    rdicts[rplural][rid] = dict(name=resource.metadata['name'],
                                                deleted=deleted)
                    status = None
                    if 'state' in resource.metadata:
                        status = resource.metadata['state']
                    elif 'status' in resource.metadata:
                        status = resource.metadata['status']
                        pass
                    if not status is None:
                        rdicts[rplural][rid]['status'] = str(status).lower()
                        pass
                    pass
                if rplural:
                    rname = rplural
                else:
                    rname = 'resources'
                    pass
                if not hostname in mdict:
                    mdict[hostname] = { 'total':0.0,rname:{} }
                    pass
                mdict[hostname]['total'] += stat.sum
                mdict[hostname][rname][rid] = stat.sum
                pass
            periods[period][meter] = mdict
            pass
        for (res,infodict) in rdicts.iteritems():
            # If we haven't seen this resource before, slap all
            # the info we've found for all those resource ids
            # into our info dict for this resource type.  Else,
            # carefully merge -- if we've already collected info
            # for a specific resource, don't overwrite that.
            # The theory for the Else case is that newer info
            # is better, if the older info differs (which I can't
            # think right now it would... should be the same for
            # all periods).
            if not res in info:
                info[res] = infodict
            else:
                for (resid,resinfodict) in infodict.iteritems():
                    if not resid in info[res]:
                        info[res][resid] = resinfodict
                pass
            pass
        pass
    
    info['host2vname'] = vhostnames
    info['host2pnode'] = phostnames

    ett = time.gmtime()
    ect = time.mktime(ett)
    ects = time.strftime('%Y-%m-%dT%H:%M:%S',ett)
    gmoffset = time.timezone
    daylight = False
    if time.daylight:
        gmoffset = time.altzone
        daylight = True
        pass

    metadata = dict(start=cts,start_timestamp=ct,
                    end=ects,end_timestamp=ect,
                    duration=(ect-ct),gmoffset=gmoffset,
                    daylight=daylight,version=VERSION)
    
    return dict(periods=periods,info=info,META=metadata)

def preload_resources(client):
    global resources

    resourcelist = client.resources.list(limit=LIMIT)
    LOG.debug("Resources: " + pp.pformat(resourcelist))
    for r in resourcelist:
        resources[r.id] = r
        pass
    pass

def reload_hostnames():
    global vhostnames,phostnames,OURDOMAIN
    newvhostnames = {}
    newphostnames = {}
    
    try:
        f = file(OURDIR + "/fqdn.map")
        i = 0
        for line in f:
            i += 1
            if len(line) == 0 or line[0] == '#':
                continue
            line = line.rstrip('\n')
            la = line.split("\t")
            if len(la) != 2:
                LOG.warn("bad FQDN line %d; skipping" % (i,))
                continue
            vname = la[0].lower()
            fqdn = la[1].lower()
            newvhostnames[fqdn] = vname
            if OURDOMAIN is None or OURDOMAIN == '':
                idx = fqdn.find('.')
                if idx > -1:
                    OURDOMAIN = fqdn[idx+1:]
                pass
            pass
        vhostnames = newvhostnames
        
        f = file(OURDIR + "/fqdn.physical.map")
        i = 0
        for line in f:
            i += 1
            if len(line) == 0 or line[0] == '#':
                continue
            line = line.rstrip('\n')
            la = line.split("\t")
            if len(la) != 2:
                LOG.warn("bad FQDN line %d; skipping" % (i,))
                continue
            pname = la[0].lower()
            fqdn = la[1].lower()
            newphostnames[fqdn] = pname
            pass
        phostnames = newphostnames
    except:
        LOG.exception("failed to reload hostnames, returning None")
        pass
    return

def main():
    try:
        os.makedirs(OUTDIR)
    except:
        pass
    kargs = build_keystone_args()
    LOG.debug("keystone args: %s" % (str(kargs)))
    
    cclient = client.get_client(2,**kargs)
    
    if False:
        preload_resources(cclient)
        pass
    
    iteration = 0
    outfile = "%s/%s" % (OUTDIR,OUTBASENAME)
    tmpoutfile = outfile + ".NEW"
    while True:
        iteration += 1
        try:
            reload_hostnames()
            newdatadict = fetchall(cclient)
            f = file(tmpoutfile,'w')
            f.write(json.dumps(newdatadict,sort_keys=True,indent=4) + "\n")
            f.close()
            shutil.move(tmpoutfile,outfile)
        except:
            LOG.exception("failure during iteration %d; nothing new generated"
                          % (iteration,))
            pass
        
        time.sleep(2 * MINUTE)
        pass

    #meters = client.meters.list(limit=LIMIT)
    #pp.pprint("Meters: ")
    #pp.pprint(meters)
    
    # Ceilometer meter.list command only allows filtering on
    # ['project', 'resource', 'source', 'user'].
    # q=[{'field':'meter.name','value':'cpu_util','op':'eq','type':'string'}]
    #cpu_util_meters = []
    #for m in meters:
    #    if m.name == 'cpu_util':
    #        cpu_util_meters.append(m)
    #    pass
    #pp.pprint("cpu_util Meters:")
    #pp.pprint(cpu_util_meters)
    
    #for m in cpu_util_meters:
    #    pp.pprint("Resource %s for cpu_util meter %s:" % (m.resource_id,m.meter_id))
    #    pp.pprint(resources[m.resource_id])
    #    pass
    
    return -1

if __name__ == "__main__":
    main()
