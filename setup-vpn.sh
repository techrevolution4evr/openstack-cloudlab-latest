#!/bin/sh

##
## Setup OpenVPN to create the OpenStack management network.
## This script only runs on the "network" node.
##

set -x

# Gotta know the rules!
if [ $EUID -ne 0 ] ; then
    echo "This script must be run as root" 1>&2
    exit 1
fi

DIRNAME=`dirname $0`

# Grab our libs
. "$DIRNAME/setup-lib.sh"

if [ "$HOSTNAME" != "$NETWORKMANAGER" ]; then
    exit 0;
fi

logtstart "vpn"

if [ ! -f $OURDIR/vpn-server-done ]; then
    maybe_install_packages openvpn easy-rsa
fi

# Only copy files later on to new nodes...
NEWVPNNODES=""

#
# Get our server CA config set up.
#
export EASY_RSA="/etc/openvpn/easy-rsa"

if [ -e /usr/share/easy-rsa/build-ca ]; then
    OLDEASYRSA=1
else
    OLDEASYRSA=0
fi

if [ ! -f $OURDIR/vpn-server-done ]; then
    if [ $OLDEASYRSA -eq 1 ]; then
	mkdir -p $EASY_RSA
	cp -r /usr/share/easy-rsa/* $EASY_RSA
	cd $EASY_RSA
	# Batch mode
	sed -i -e s/--interact/--batch/ $EASY_RSA/build-ca
	sed -i -e s/--interact/--batch/ $EASY_RSA/build-key-server
	sed -i -e s/--interact/--batch/ $EASY_RSA/build-key
	sed -i -e s/DEBUG=0/DEBUG=1/ $EASY_RSA/pkitool
    else
	make-cadir $EASY_RSA
	cd $EASY_RSA
    fi
fi

TRUNCATED_EMAIL=`echo ${SWAPPER_EMAIL} | cut -c 1-40`
if [ $OLDEASYRSA -eq 1 ]; then
    export OPENSSL="openssl"
    export PKCS11TOOL="pkcs11-tool"
    export GREP="grep"
    export KEY_CONFIG="`$EASY_RSA/whichopensslcnf $EASY_RSA`"
    export KEY_DIR="$EASY_RSA/keys"
    export PKCS11_MODULE_PATH="dummy"
    export PKCS11_PIN="dummy"
    export KEY_SIZE=2048
    export CA_EXPIRE=3650
    export KEY_EXPIRE=3650

    export KEY_COUNTRY="US"
    export KEY_PROVINCE="UT"
    export KEY_CITY="Salt Lake City"
    export KEY_ORG="$EPID-$EEID"
    export KEY_EMAIL="${TRUNCATED_EMAIL}"
    export KEY_CN="OSMgmtVPN"
    export KEY_NAME=$KEY_CN
    export KEY_OU=$KEY_CN
    # --batch mode is unhappy if it's not this
    export KEY_ALTNAMES="DNS:$NETWORKMANAGER"
else
    cat <<EOF >>$EASY_RSA/vars
set_var EASYRSA_REQ_COUNTRY    "US"
set_var EASYRSA_REQ_PROVINCE   "UT"
set_var EASYRSA_REQ_CITY       "Salt Lake City"
set_var EASYRSA_REQ_ORG        "$EPID-$EEID"
set_var EASYRSA_REQ_EMAIL      "${TRUNCATED_EMAIL}"
set_var EASYRSA_REQ_CN         "OSMgmtVPN"
set_var EASYRSA_REQ_OU         "OSMgmtVPN"
set_var EASYRSA_KEY_SIZE       2048
set_var EASYRSA_CA_EXPIRE      3650
set_var EASYRSA_CERT_EXPIRE    3650
set_var EASYRSA_BATCH          "1"
EOF
    export EASYRSA_REQ_COUNTRY="US"
    export EASYRSA_REQ_PROVINCE="UT"
    export EASYRSA_REQ_CITY="Salt Lake City"
    export EASYRSA_REQ_ORG="$EPID-$EEID"
    export EASYRSA_REQ_EMAIL="${TRUNCATED_EMAIL}"
    export EASYRSA_REQ_CN="OSMgmtVPN"
    export EASYRSA_REQ_OU="OSMgmtVPN"
    export EASYRSA_KEY_SIZE=2048
    export EASYRSA_CA_EXPIRE=3650
    export EASYRSA_CERT_EXPIRE=3650
    export EASYRSA_BATCH="1"
fi

mkdir -p $KEY_DIR
cd $EASY_RSA

if [ ! -f $OURDIR/vpn-server-done ]; then
    # Handle the case on Ubuntu18 where easy-rsa is broken for openssl 1.1.0
    # (https://github.com/OpenVPN/easy-rsa/issues/159)
    openssl version | grep -iq '^openssl 1\.1\.'
    if [ $? -eq 0 -a $OLDEASYRSA -eq 1 -a -e openssl-1.0.0.cnf -a ! -e $KEY_CONFIG ]; then
	    cp -p openssl-1.0.0.cnf $KEY_CONFIG
	    echo '# For use with easy-rsa version 2.x and OpenSSL 1.1.0*' >> $KEY_CONFIG
	    echo '# For use with easy-rsa version 2.0 and OpenSSL 1.1.0*' >> $KEY_CONFIG
    fi

    # Fixup the openssl.cnf files
    for file in `ls -1 /etc/openvpn/easy-rsa/openssl*.cnf | xargs` ; do
	sed -i -e 's/^\(subjectAltName=.*\)$/#\1/' $file
    done

    if [ $OLDEASYRSA -eq 1 ]; then
	./clean-all
	./build-ca
	# We needed a CN for the CA build -- but now we have to drop it cause
	# the build-key* scripts don't want it set -- they set it to the first arg,
	# and behave badly if it IS set.
	unset KEY_CN
	./build-key-server $NETWORKMANAGER
	cp -p $KEY_DIR/$NETWORKMANAGER.crt $KEY_DIR/$NETWORKMANAGER.key $KEY_DIR/ca.crt \
	    /etc/openvpn/

	if [ -f $DIRNAME/etc/dh2048.pem ]; then
	    cp $DIRNAME/etc/dh2048.pem /etc/openvpn
	else
	    ./build-dh
	    cp -p $KEY_DIR/dh2048.pem /etc/openvpn/
	fi
    else
	./easyrsa init-pki
	./easyrsa build-ca nopass
	./easyrsa build-server-full $NETWORKMANAGER nopass
	cp -p $EASY_RSA/pki/issued/$NETWORKMANAGER.crt \
	    $EASY_RSA/pki/private/$NETWORKMANAGER.key \
	    $EASY_RSA/pki/ca.crt \
	    /etc/openvpn

	if [ -f $DIRNAME/etc/dh2048.pem ]; then
	    cp $DIRNAME/etc/dh2048.pem /etc/openvpn
	else
	    ./easyrsa gen-dh
	    cp -p $EASY_RSA/pki/dh.pem /etc/openvpn/dh2048.pem
	fi
    fi

    #
    # Get openvpn setup and restarted.
    #
    cat <<EOF > /etc/openvpn/server.conf
local $MYIP
port 1194
proto udp
dev tun
ca ca.crt
cert $NETWORKMANAGER.crt
key $NETWORKMANAGER.key
dh dh2048.pem
server 192.168.0.0 255.255.0.0
client-config-dir /etc/openvpn/ccd
client-to-client
;duplicate-cn
keepalive 10 120
comp-lzo
persist-key
persist-tun
status openvpn-status.log
verb 3
EOF

    mkdir -p /etc/openvpn/ccd

    #
    # Get the server up
    #
    if [ ${HAVE_SYSTEMD} -eq 1 ]; then
	# Make sure we don't start the VPN until our network is up.
	# This is sort of magical, but it works.
	mkdir /etc/systemd/system/openvpn@.service.d
	systemctl list-units | grep -q networking\.service
	if [ $? -eq 0 ]; then
	    cat <<EOF >/etc/systemd/system/openvpn@.service.d/local-ifup.conf
[Unit]
Requires=networking.service
After=networking.service
EOF
	else
	    systemctl list-units | grep -q network-online\.target
	    if [ $? -eq 0 ]; then
		cat <<EOF >/etc/systemd/system/openvpn@.service.d/local-ifup.conf
[Unit]
Requires=network-online.target
After=network-online.target
EOF
	    fi
	fi
	systemctl daemon-reload
	systemctl enable openvpn@server.service
	systemctl start openvpn@server.service
    else
	service openvpn restart
    fi

    touch $OURDIR/vpn-server-done
fi

#
# Now build keys and set static IPs for the controller and the
# compute nodes.
#
for node in $NODES
do
    if [ -f /etc/openvpn/ccd/$node ]; then
	continue
    fi

    NEWVPNNODES="${NEWVPNNODES} $node"

    fqdn=`getfqdn $node`

    if [ $OLDEASYRSA -eq 1 ]; then
	export KEY_CN="$node"
	./build-key $node
    else
	./easyrsa build-client-full "$node" nopass
    fi

    NMIP=`cat $OURDIR/mgmt-hosts | grep -E "$node$" | head -1 | sed -n -e 's/^\\([0-9]*\\.[0-9]*\\.[0-9]*\\.[0-9]*\\).*$/\\1/p'`
    echo "ifconfig-push $NMIP 255.255.0.0" \
	> /etc/openvpn/ccd/$node
done

if [ $OLDEASYRSA -eq 1 ]; then
    unset KEY_COUNTRY
    unset KEY_PROVINCE
    unset KEY_CITY
    unset KEY_ORG
    unset KEY_EMAIL
    unset KEY_NAME
    unset KEY_OU
    unset KEY_CN
    unset KEY_ALTNAMES

    unset OPENSSL
    unset PKCS11TOOL
    unset GREP
    unset KEY_CONFIG
    unset PKCS11_MODULE_PATH
    unset PKCS11_PIN
    unset KEY_SIZE
    unset CA_EXPIRE
    unset KEY_EXPIRE
fi

#
# Get the hosts files setup to point to the new management network
# and setup the VPN on the clients.
#
maybe_install_packages pssh
PSSH='/usr/bin/parallel-ssh -t 0 -O StrictHostKeyChecking=no '
PHOSTS=""
mkdir -p $OURDIR/pssh.setup-vpn.stdout $OURDIR/pssh.setup-vpn.stderr

for node in $NEWVPNNODES
do
    [ "$node" = "$NETWORKMANAGER" ] && continue

    fqdn=`getfqdn $node`
    $SSH $fqdn mkdir -p $OURDIR
    if [ $OLDEASYRSA -eq 1 ]; then
	scp -p -o StrictHostKeyChecking=no \
	    /etc/openvpn/ca.crt $KEY_DIR/$node.crt $KEY_DIR/$node.key \
	    $fqdn:$OURDIR
    else
	scp -p -o StrictHostKeyChecking=no \
	    /etc/openvpn/ca.crt \
	    $EASY_RSA/pki/issued/$node.crt \
	    $EASY_RSA/pki/private/$node.key \
	    $fqdn:$OURDIR
    fi
    PHOSTS="$PHOSTS -H $fqdn"
done

$PSSH -o $OURDIR/pssh.setup-vpn.stdout -e $OURDIR/pssh.setup-vpn.stderr \
    $PHOSTS $DIRNAME/setup-vpn-client.sh

unset EASY_RSA

logtend "vpn"

exit 0
