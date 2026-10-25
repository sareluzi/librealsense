// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

#include "modules.h"

#ifdef _WIN32
#include <windows.h>
#else
#include <dlfcn.h>
#endif

namespace rs2_gentl {

registry & registry::instance()
{
    static registry r;
    return r;
}

void registry::collect( module * m, std::vector< std::shared_ptr< module > > & order )
{
    for( auto & h : _handles )
        if( h.first->parent() == m )
            collect( h.first, order );
    auto it = _handles.find( m );
    if( it != _handles.end() )
        order.push_back( it->second );
}

void registry::close( module * m )
{
    std::vector< std::shared_ptr< module > > order;
    {
        std::lock_guard< std::mutex > lock( _mutex );
        collect( m, order );
    }
    // on_close may stop sensors and wait for their callbacks, so it runs without the registry lock
    for( auto & x : order )
        x->on_close();
    std::lock_guard< std::mutex > lock( _mutex );
    for( auto & x : order )
        _handles.erase( x.get() );
}

void registry::clear()
{
    std::vector< module * > roots;
    {
        std::lock_guard< std::mutex > lock( _mutex );
        for( auto & h : _handles )
            if( ! h.first->parent() )
                roots.push_back( h.first );
    }
    for( auto root : roots )
        close( root );
}

namespace {

std::string library_path()
{
#ifdef _WIN32
    HMODULE module = nullptr;
    char path[MAX_PATH] = {};
    if( GetModuleHandleExA( GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast< LPCSTR >( &library_path ), &module ) )
        GetModuleFileNameA( module, path, MAX_PATH );
    return path;
#else
    Dl_info info = {};
    return dladdr( reinterpret_cast< void * >( &library_path ), &info ) && info.dli_fname ? info.dli_fname : "";
#endif
}

std::string file_name( const std::string & path )
{
    auto pos = path.find_last_of( "/\\" );
    return pos == std::string::npos ? path : path.substr( pos + 1 );
}

std::string sdk_version()
{
    rs2_error * e = nullptr;
    int v = rs2_get_api_version( &e );
    return std::to_string( v / 10000 ) + "." + std::to_string( ( v / 100 ) % 100 ) + "." + std::to_string( v % 100 );
}

}  // namespace

system_module::system_module()
    : module( nullptr )
{
    _port.reset( new node_map( "TLPort", "RealSense_GenTL_System", "6A8E1C02-5B1D-4C55-9C1E-3F6D9B0A7E11" ) );
    auto & map = *_port;
    auto info_string = []( TL_INFO_CMD cmd ) {
        return [cmd]() {
            char value[1024] = {};
            size_t size = sizeof( value );
            info( cmd, nullptr, value, &size );
            return std::string( value );
        };
    };
    map.add_string( "TLVendorName", info_string( TL_INFO_VENDOR ) );
    map.add_string( "TLModelName", info_string( TL_INFO_MODEL ) );
    map.add_string( "TLID", info_string( TL_INFO_ID ) );
    map.add_string( "TLVersion", info_string( TL_INFO_VERSION ) );
    map.add_string( "TLType", info_string( TL_INFO_TLTYPE ) );
    map.add_integer( "GenTLVersionMajor", []( int ) { return int64_t( GenTLMajorVersion ); }, nullptr );
    map.add_integer( "GenTLVersionMinor", []( int ) { return int64_t( GenTLMinorVersion ); }, nullptr );
    map.add_category( "SystemInformation", { "TLVendorName", "TLModelName", "TLID", "TLVersion", "TLType",
                                             "GenTLVersionMajor", "GenTLVersionMinor" } );
    map.add_category( "Root", { "SystemInformation" } );
    map.finalize();
}

void system_module::info( TL_INFO_CMD cmd, INFO_DATATYPE * type, void * buffer, size_t * size )
{
    switch( cmd )
    {
    case TL_INFO_ID: return info_string( type, buffer, size, TL_ID );
    case TL_INFO_VENDOR: return info_string( type, buffer, size, VENDOR_NAME );
    case TL_INFO_MODEL: return info_string( type, buffer, size, "RealSense GenTL Producer" );
    case TL_INFO_VERSION: return info_string( type, buffer, size, sdk_version() );
    case TL_INFO_TLTYPE: return info_string( type, buffer, size, TL_TYPE );
    case TL_INFO_NAME: return info_string( type, buffer, size, file_name( library_path() ) );
    case TL_INFO_PATHNAME: return info_string( type, buffer, size, library_path() );
    case TL_INFO_DISPLAYNAME: return info_string( type, buffer, size, "RealSense GenTL Producer" );
    case TL_INFO_CHAR_ENCODING: return info_value< int32_t >( INFO_DATATYPE_INT32, type, buffer, size, TL_CHAR_ENCODING_ASCII );
    case TL_INFO_GENTL_VER_MAJOR: return info_value< uint32_t >( INFO_DATATYPE_UINT32, type, buffer, size, GenTLMajorVersion );
    case TL_INFO_GENTL_VER_MINOR: return info_value< uint32_t >( INFO_DATATYPE_UINT32, type, buffer, size, GenTLMinorVersion );
    default: unknown_info( cmd );
    }
}

interface_module::interface_module( system_module * parent )
    : module( parent )
{
    _port.reset( new node_map( "TLPort", "RealSense_GenTL_Interface", "0F3B6D7A-9E42-4B8C-A1D5-6C2E8F4B9A13" ) );
    auto & map = *_port;
    map.add_string( "InterfaceID", [] { return std::string( INTERFACE_ID ); } );
    map.add_string( "InterfaceType", [] { return std::string( TL_TYPE ); } );
    map.add_category( "InterfaceInformation", { "InterfaceID", "InterfaceType" } );
    map.add_category( "Root", { "InterfaceInformation" } );
    map.finalize();
}

void interface_module::info( INTERFACE_INFO_CMD cmd, INFO_DATATYPE * type, void * buffer, size_t * size )
{
    switch( cmd )
    {
    case INTERFACE_INFO_ID: return info_string( type, buffer, size, INTERFACE_ID );
    case INTERFACE_INFO_DISPLAYNAME: return info_string( type, buffer, size, "RealSense devices" );
    case INTERFACE_INFO_TLTYPE: return info_string( type, buffer, size, TL_TYPE );
    default: unknown_info( cmd );
    }
}

bool interface_module::update_devices()
{
    auto list = static_cast< system_module * >( parent() )->context().query_devices( RS2_PRODUCT_LINE_DEPTH );
    std::vector< std::pair< std::string, rs2::device > > devices;
    for( auto && dev : list )
        if( dev.supports( RS2_CAMERA_INFO_SERIAL_NUMBER ) )
            devices.emplace_back( dev.get_info( RS2_CAMERA_INFO_SERIAL_NUMBER ), dev );

    std::lock_guard< std::mutex > lock( _mutex );
    bool changed = devices.size() != _devices.size();
    for( size_t i = 0; ! changed && i < devices.size(); ++i )
        changed = devices[i].first != _devices[i].first;
    _devices = std::move( devices );
    return changed;
}

std::vector< std::string > interface_module::device_ids()
{
    std::lock_guard< std::mutex > lock( _mutex );
    std::vector< std::string > ids;
    for( auto & d : _devices )
        ids.push_back( d.first );
    return ids;
}

rs2::device interface_module::find( const std::string & id )
{
    std::lock_guard< std::mutex > lock( _mutex );
    for( auto & d : _devices )
        if( d.first == id )
            return d.second;
    throw gentl_error( GC_ERR_INVALID_ID, "unknown device ID '" + id + "'" );
}

void interface_module::device_info( const std::string & id, DEVICE_INFO_CMD cmd, INFO_DATATYPE * type, void * buffer,
                                    size_t * size )
{
    for( auto & open : registry::instance().children< device_module >( this ) )
        if( open->id() == id )
            return open->info( cmd, type, buffer, size );
    rs2::device dev = find( id );
    if( cmd == DEVICE_INFO_ACCESS_STATUS )
        return info_value< int32_t >( INFO_DATATYPE_INT32, type, buffer, size, DEVICE_ACCESS_STATUS_READWRITE );
    device_module::identity_info( dev, cmd, type, buffer, size );
}

std::shared_ptr< device_module > interface_module::open_device( const std::string & id )
{
    for( auto & open : registry::instance().children< device_module >( this ) )
        if( open->id() == id )
            throw gentl_error( GC_ERR_RESOURCE_IN_USE, "device " + id + " is already open" );
    return std::make_shared< device_module >( this, find( id ) );
}

}  // namespace rs2_gentl
