#!/bin/sh

##
## Setup a root ssh key on the calling node, and broadcast it to all the
## other nodes' authorized_keys file.
##

set -x

# Gotta know the rules!
if [ $EUID -ne 0 ] ; then
    echo "This script must be run as root" 1>&2
    exit 1
fi

# Grab our libs
. "`dirname $0`/setup-lib.sh"

KEYNAME=id_rsa

# Remove it if it exists...
rm -f ~/.ssh/${KEYNAME} ~/.ssh/${KEYNAME}.pub

##
## Figure out our strategy.  Are we using the new geni_certificate and
## geni_key support to generate the same keypair on each host, or not.
##
geni-get key > $OURDIR/$KEYNAME
chmod 600 $OURDIR/${KEYNAME}
if [ -s $OURDIR/${KEYNAME} ] ; then
    ssh-keygen -f $OURDIR/${KEYNAME} -y > $OURDIR/${KEYNAME}.pub
    chmod 600 $OURDIR/${KEYNAME}.pub
    mkdir -p ~/.ssh
    chmod 600 ~/.ssh
    cp -p $OURDIR/${KEYNAME} $OURDIR/${KEYNAME}.pub ~/.ssh/
    cat $OURDIR/${KEYNAME}.pub >> ~/.ssh/authorized_keys
    chmod 600 ~/.ssh/authorized_keys
    exit 0
fi

##
## If geni calls are not available, make ourself a keypair; this gets copied
## to other roots' authorized_keys.
##
if [ ! -f /root/.ssh/${KEYNAME} ]; then
    ssh-keygen -t rsa -f /root/.ssh/${KEYNAME} -N ''
fi

if [ "$SWAPPER" = "geniuser" ]; then
    SHAREDIR=/proj/$EPID/exp/$EEID/tmp

    cp /root/.ssh/${KEYNAME}.pub $SHAREDIR/$HOSTNAME

    for node in $NODES ; do
	while [ ! -f $SHAREDIR/$node ]; do
            sleep 1
	done
	echo $node is up
	cat $SHAREDIR/$node >> /root/.ssh/authorized_keys
    done
else
    for node in $NODES ; do
	if [ "$node" != "$HOSTNAME" ]; then 
	    fqdn="$node.$EEID.$EPID.$OURDOMAIN"
	    SUCCESS=1
	    while [ $SUCCESS -ne 0 ]; do
		su -c "$SSH  -l $SWAPPER $fqdn sudo tee -a /root/.ssh/authorized_keys" $SWAPPER < /root/.ssh/${KEYNAME}.pub
		SUCCESS=$?
		sleep 1
	    done
	fi
    done
fi

exit 0
