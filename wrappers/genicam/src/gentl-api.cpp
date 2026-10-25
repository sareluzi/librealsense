// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

// The exported GenTL C API: validates handles, forwards to the modules and turns exceptions into GC_ERROR codes

#include "modules.h"

#include <atomic>

using namespace rs2_gentl;

namespace {

std::atomic< bool > g_initialized( false );
std::mutex g_system_mutex;
std::weak_ptr< system_module > g_system;

struct last_error
{
    GC_ERROR code = GC_ERR_SUCCESS;
    std::string text;
};
thread_local last_error g_last_error;

template< class F >
GC_ERROR guarded( F && f, bool requires_init = true )
{
    try
    {
        if( requires_init && ! g_initialized )
            throw gentl_error( GC_ERR_NOT_INITIALIZED, "GCInitLib was not called" );
        f();
        return GC_ERR_SUCCESS;
    }
    catch( const gentl_error & e )
    {
        g_last_error = { e.code(), e.what() };
    }
    catch( const rs2::error & e )
    {
        g_last_error = { GC_ERR_IO, e.get_failed_function() + "(" + e.get_failed_args() + "): " + e.what() };
    }
    catch( const std::exception & e )
    {
        g_last_error = { GC_ERR_ERROR, e.what() };
    }
    catch( ... )
    {
        g_last_error = { GC_ERR_ERROR, "unknown error" };
    }
    return g_last_error.code;
}

template< class T >
std::shared_ptr< T > get( const void * handle )
{
    return registry::instance().get< T >( handle );
}

template< class T >
void * handle( const std::shared_ptr< T > & m )
{
    return static_cast< module * >( m.get() );
}

template< class T >
void set_out( T * out, T value )
{
    if( ! out )
        throw gentl_error( GC_ERR_INVALID_PARAMETER, "output pointer is null" );
    *out = value;
}

std::string as_string( const char * s )
{
    if( ! s )
        throw gentl_error( GC_ERR_INVALID_PARAMETER, "ID is null" );
    return s;
}

// Returns the module, not its node map, so the caller keeps the port alive for the whole call
std::shared_ptr< module > port_of( PORT_HANDLE h )
{
    auto m = get< module >( h );
    if( ! m->port() )
        throw gentl_error( GC_ERR_INVALID_HANDLE, "handle is not a port" );
    return m;
}

std::shared_ptr< data_stream > stream_of_buffer( DS_HANDLE hStream, BUFFER_HANDLE hBuffer, std::shared_ptr< buffer > & b )
{
    auto ds = get< data_stream >( hStream );
    b = get< buffer >( hBuffer );
    if( b->parent() == ds.get() )
        return ds;
    // Harvesters 1.4 uses the wrong stream handle for buffers when a device has several; route to the owner
    auto owner = get< data_stream >( b->parent() );
    if( owner->parent() != ds->parent() )
        throw gentl_error( GC_ERR_INVALID_HANDLE, "buffer belongs to another device" );
    return owner;
}

}  // namespace

namespace GenTL {

GC_API GCGetInfo( TL_INFO_CMD iInfoCmd, INFO_DATATYPE * piType, void * pBuffer, size_t * piSize )
{
    return guarded( [&] { system_module::info( iInfoCmd, piType, pBuffer, piSize ); }, false );
}

GC_API GCGetLastError( GC_ERROR * piErrorCode, char * sErrText, size_t * piSize )
{
    last_error error = g_last_error;
    return guarded(
        [&] {
            set_out( piErrorCode, error.code );
            info_string( nullptr, sErrText, piSize, error.text );
        },
        false );
}

GC_API GCInitLib( void )
{
    return guarded(
        [] {
            if( g_initialized.exchange( true ) )
                throw gentl_error( GC_ERR_RESOURCE_IN_USE, "GCInitLib was already called" );
        },
        false );
}

GC_API GCCloseLib( void )
{
    return guarded( [] {
        registry::instance().clear();
        g_initialized = false;
    } );
}

GC_API GCReadPort( PORT_HANDLE hPort, uint64_t iAddress, void * pBuffer, size_t * piSize )
{
    return guarded( [&] {
        if( ! pBuffer || ! piSize )
            throw gentl_error( GC_ERR_INVALID_PARAMETER, "null buffer" );
        port_of( hPort )->port()->read( iAddress, pBuffer, *piSize );
    } );
}

GC_API GCWritePort( PORT_HANDLE hPort, uint64_t iAddress, const void * pBuffer, size_t * piSize )
{
    return guarded( [&] {
        if( ! pBuffer || ! piSize )
            throw gentl_error( GC_ERR_INVALID_PARAMETER, "null buffer" );
        port_of( hPort )->port()->write( iAddress, pBuffer, *piSize );
    } );
}

GC_API GCReadPortStacked( PORT_HANDLE hPort, PORT_REGISTER_STACK_ENTRY * pEntries, size_t * piNumEntries )
{
    return guarded( [&] {
        if( ! pEntries || ! piNumEntries )
            throw gentl_error( GC_ERR_INVALID_PARAMETER, "null entries" );
        auto m = port_of( hPort );
        for( size_t i = 0; i < *piNumEntries; ++i )
            m->port()->read( pEntries[i].Address, pEntries[i].pBuffer, pEntries[i].Size );
    } );
}

GC_API GCWritePortStacked( PORT_HANDLE hPort, PORT_REGISTER_STACK_ENTRY * pEntries, size_t * piNumEntries )
{
    return guarded( [&] {
        if( ! pEntries || ! piNumEntries )
            throw gentl_error( GC_ERR_INVALID_PARAMETER, "null entries" );
        auto m = port_of( hPort );
        for( size_t i = 0; i < *piNumEntries; ++i )
            m->port()->write( pEntries[i].Address, pEntries[i].pBuffer, pEntries[i].Size );
    } );
}

GC_API GCGetPortURL( PORT_HANDLE hPort, char * sURL, size_t * piSize )
{
    return guarded( [&] { info_string( nullptr, sURL, piSize, port_of( hPort )->port()->url() ); } );
}

GC_API GCGetNumPortURLs( PORT_HANDLE hPort, uint32_t * piNumURLs )
{
    return guarded( [&] {
        port_of( hPort );
        set_out( piNumURLs, 1u );
    } );
}

GC_API GCGetPortURLInfo( PORT_HANDLE hPort, uint32_t iURLIndex, URL_INFO_CMD iInfoCmd, INFO_DATATYPE * piType,
                         void * pBuffer, size_t * piSize )
{
    return guarded( [&] {
        auto m = port_of( hPort );
        auto & port = *m->port();
        if( iURLIndex != 0 )
            throw gentl_error( GC_ERR_INVALID_INDEX, "only one URL" );
        auto i32 = [&]( int32_t v ) { info_value( INFO_DATATYPE_INT32, piType, pBuffer, piSize, v ); };
        switch( iInfoCmd )
        {
        case URL_INFO_URL: return info_string( piType, pBuffer, piSize, port.url() );
        case URL_INFO_SCHEMA_VER_MAJOR: return i32( 1 );
        case URL_INFO_SCHEMA_VER_MINOR: return i32( 1 );
        case URL_INFO_FILE_VER_MAJOR: return i32( 1 );
        case URL_INFO_FILE_VER_MINOR: return i32( 0 );
        case URL_INFO_FILE_VER_SUBMINOR: return i32( 0 );
        case URL_INFO_FILE_REGISTER_ADDRESS: return info_u64( piType, pBuffer, piSize, port.xml_address() );
        case URL_INFO_FILE_SIZE: return info_u64( piType, pBuffer, piSize, port.xml().size() );
        case URL_INFO_SCHEME: return i32( URL_SCHEME_LOCAL );
        case URL_INFO_FILENAME: return info_string( piType, pBuffer, piSize, port.xml_file_name() );
        default: unknown_info( iInfoCmd );
        }
    } );
}

GC_API GCGetPortInfo( PORT_HANDLE hPort, PORT_INFO_CMD iInfoCmd, INFO_DATATYPE * piType, void * pBuffer, size_t * piSize )
{
    return guarded( [&] {
        auto m = get< module >( hPort );
        if( ! m->port() )
            throw gentl_error( GC_ERR_INVALID_HANDLE, "handle is not a port" );
        switch( iInfoCmd )
        {
        case PORT_INFO_ID: return info_string( piType, pBuffer, piSize, m->id() );
        case PORT_INFO_VENDOR: return info_string( piType, pBuffer, piSize, VENDOR_NAME );
        case PORT_INFO_MODEL: return info_string( piType, pBuffer, piSize, "RealSense GenTL Producer" );
        case PORT_INFO_TLTYPE: return info_string( piType, pBuffer, piSize, TL_TYPE );
        case PORT_INFO_MODULE: return info_string( piType, pBuffer, piSize, m->module_name() );
        case PORT_INFO_LITTLE_ENDIAN: return info_bool( piType, pBuffer, piSize, true );
        case PORT_INFO_BIG_ENDIAN: return info_bool( piType, pBuffer, piSize, false );
        case PORT_INFO_ACCESS_READ: return info_bool( piType, pBuffer, piSize, true );
        case PORT_INFO_ACCESS_WRITE: return info_bool( piType, pBuffer, piSize, true );
        case PORT_INFO_ACCESS_NA: return info_bool( piType, pBuffer, piSize, false );
        case PORT_INFO_ACCESS_NI: return info_bool( piType, pBuffer, piSize, false );
        case PORT_INFO_VERSION: return info_string( piType, pBuffer, piSize, "1.0" );
        case PORT_INFO_PORTNAME: return info_string( piType, pBuffer, piSize, m->port()->port_name() );
        default: unknown_info( iInfoCmd );
        }
    } );
}

GC_API GCRegisterEvent( EVENTSRC_HANDLE hEventSrc, EVENT_TYPE iEventID, EVENT_HANDLE * phEvent )
{
    return guarded( [&] {
        if( ! phEvent )
            throw gentl_error( GC_ERR_INVALID_PARAMETER, "null event handle pointer" );
        auto m = get< module >( hEventSrc );
        auto ds = std::dynamic_pointer_cast< data_stream >( m );
        if( ! ds || iEventID != EVENT_NEW_BUFFER )
            throw gentl_error( GC_ERR_NOT_IMPLEMENTED, "only NEW_BUFFER events on data streams are supported" );
        if( ! registry::instance().children< event >( ds.get() ).empty() )
            throw gentl_error( GC_ERR_RESOURCE_IN_USE, "event already registered" );
        ds->reset_kills();
        *phEvent = handle( registry::instance().add( std::make_shared< event >( ds ) ) );
    } );
}

GC_API GCUnregisterEvent( EVENTSRC_HANDLE hEventSrc, EVENT_TYPE iEventID )
{
    return guarded( [&] {
        auto m = get< module >( hEventSrc );
        auto events = registry::instance().children< event >( m.get() );
        if( events.empty() || iEventID != EVENT_NEW_BUFFER )
            throw gentl_error( GC_ERR_NOT_AVAILABLE, "event is not registered" );
        events.front()->stream->kill();
        registry::instance().close( events.front().get() );
    } );
}

GC_API EventGetData( EVENT_HANDLE hEvent, void * pBuffer, size_t * piSize, uint64_t iTimeout )
{
    return guarded( [&] {
        auto e = get< event >( hEvent );
        if( ! pBuffer || ! piSize || *piSize < sizeof( EVENT_NEW_BUFFER_DATA ) )
            throw gentl_error( GC_ERR_BUFFER_TOO_SMALL, "EVENT_NEW_BUFFER_DATA buffer needed" );
        EVENT_NEW_BUFFER_DATA data;
        e->stream->wait( data, iTimeout );
        std::memcpy( pBuffer, &data, sizeof( data ) );
        *piSize = sizeof( data );
    } );
}

GC_API EventGetDataInfo( EVENT_HANDLE hEvent, const void * pInBuffer, size_t iInSize, EVENT_DATA_INFO_CMD iInfoCmd,
                         INFO_DATATYPE * piType, void * pOutBuffer, size_t * piOutSize )
{
    return guarded( [&] {
        get< event >( hEvent );
        if( ! pInBuffer || iInSize < sizeof( EVENT_NEW_BUFFER_DATA ) )
            throw gentl_error( GC_ERR_INVALID_PARAMETER, "EVENT_NEW_BUFFER_DATA expected" );
        EVENT_NEW_BUFFER_DATA data;
        std::memcpy( &data, pInBuffer, sizeof( data ) );
        switch( iInfoCmd )
        {
        case EVENT_DATA_ID: return info_ptr( piType, pOutBuffer, piOutSize, data.BufferHandle );
        case EVENT_DATA_VALUE: return info_ptr( piType, pOutBuffer, piOutSize, data.pUserPointer );
        default: unknown_info( iInfoCmd );
        }
    } );
}

GC_API EventGetInfo( EVENT_HANDLE hEvent, EVENT_INFO_CMD iInfoCmd, INFO_DATATYPE * piType, void * pBuffer, size_t * piSize )
{
    return guarded( [&] {
        auto e = get< event >( hEvent );
        switch( iInfoCmd )
        {
        case EVENT_EVENT_TYPE: return info_value< int32_t >( INFO_DATATYPE_INT32, piType, pBuffer, piSize, EVENT_NEW_BUFFER );
        case EVENT_NUM_IN_QUEUE: return info_sizet( piType, pBuffer, piSize, e->stream->events_in_queue() );
        case EVENT_NUM_FIRED: return info_u64( piType, pBuffer, piSize, e->stream->events_fired() );
        case EVENT_SIZE_MAX: return info_sizet( piType, pBuffer, piSize, sizeof( EVENT_NEW_BUFFER_DATA ) );
        case EVENT_INFO_DATA_SIZE_MAX: return info_sizet( piType, pBuffer, piSize, sizeof( void * ) );
        default: unknown_info( iInfoCmd );
        }
    } );
}

GC_API EventFlush( EVENT_HANDLE hEvent )
{
    return guarded( [&] { get< event >( hEvent )->stream->flush_events(); } );
}

GC_API EventKill( EVENT_HANDLE hEvent )
{
    return guarded( [&] { get< event >( hEvent )->stream->kill(); } );
}

GC_API TLOpen( TL_HANDLE * phTL )
{
    return guarded( [&] {
        if( ! phTL )
            throw gentl_error( GC_ERR_INVALID_PARAMETER, "null handle pointer" );
        std::lock_guard< std::mutex > lock( g_system_mutex );
        if( ! g_system.expired() )
            throw gentl_error( GC_ERR_RESOURCE_IN_USE, "the system module is already open" );
        auto system = registry::instance().add( std::make_shared< system_module >() );
        g_system = system;
        *phTL = handle( system );
    } );
}

GC_API TLClose( TL_HANDLE hTL )
{
    return guarded( [&] { registry::instance().close( get< system_module >( hTL ).get() ); } );
}

GC_API TLGetInfo( TL_HANDLE hTL, TL_INFO_CMD iInfoCmd, INFO_DATATYPE * piType, void * pBuffer, size_t * piSize )
{
    return guarded( [&] { get< system_module >( hTL )->info( iInfoCmd, piType, pBuffer, piSize ); } );
}

GC_API TLGetNumInterfaces( TL_HANDLE hTL, uint32_t * piNumIfaces )
{
    return guarded( [&] {
        get< system_module >( hTL );
        set_out( piNumIfaces, 1u );
    } );
}

GC_API TLGetInterfaceID( TL_HANDLE hTL, uint32_t iIndex, char * sID, size_t * piSize )
{
    return guarded( [&] {
        get< system_module >( hTL );
        if( iIndex != 0 )
            throw gentl_error( GC_ERR_INVALID_INDEX, "only one interface" );
        info_string( nullptr, sID, piSize, INTERFACE_ID );
    } );
}

GC_API TLGetInterfaceInfo( TL_HANDLE hTL, const char * sIfaceID, INTERFACE_INFO_CMD iInfoCmd, INFO_DATATYPE * piType,
                           void * pBuffer, size_t * piSize )
{
    return guarded( [&] {
        get< system_module >( hTL );
        if( as_string( sIfaceID ) != INTERFACE_ID )
            throw gentl_error( GC_ERR_INVALID_ID, "unknown interface ID" );
        interface_module::info( iInfoCmd, piType, pBuffer, piSize );
    } );
}

GC_API TLOpenInterface( TL_HANDLE hTL, const char * sIfaceID, IF_HANDLE * phIface )
{
    return guarded( [&] {
        auto system = get< system_module >( hTL );
        if( as_string( sIfaceID ) != INTERFACE_ID )
            throw gentl_error( GC_ERR_INVALID_ID, "unknown interface ID" );
        if( ! registry::instance().children< interface_module >( system.get() ).empty() )
            throw gentl_error( GC_ERR_RESOURCE_IN_USE, "interface already open" );
        set_out( phIface, handle( registry::instance().add( std::make_shared< interface_module >( system.get() ) ) ) );
    } );
}

GC_API TLUpdateInterfaceList( TL_HANDLE hTL, bool8_t * pbChanged, uint64_t /*iTimeout*/ )
{
    return guarded( [&] {
        get< system_module >( hTL );
        if( pbChanged )
            *pbChanged = false;
    } );
}

GC_API IFClose( IF_HANDLE hIface )
{
    return guarded( [&] { registry::instance().close( get< interface_module >( hIface ).get() ); } );
}

GC_API IFGetInfo( IF_HANDLE hIface, INTERFACE_INFO_CMD iInfoCmd, INFO_DATATYPE * piType, void * pBuffer, size_t * piSize )
{
    return guarded( [&] {
        get< interface_module >( hIface );
        interface_module::info( iInfoCmd, piType, pBuffer, piSize );
    } );
}

GC_API IFGetNumDevices( IF_HANDLE hIface, uint32_t * piNumDevices )
{
    return guarded( [&] { set_out( piNumDevices, uint32_t( get< interface_module >( hIface )->device_ids().size() ) ); } );
}

GC_API IFGetDeviceID( IF_HANDLE hIface, uint32_t iIndex, char * sIDeviceID, size_t * piSize )
{
    return guarded( [&] {
        auto ids = get< interface_module >( hIface )->device_ids();
        if( iIndex >= ids.size() )
            throw gentl_error( GC_ERR_INVALID_INDEX, "device index out of range" );
        info_string( nullptr, sIDeviceID, piSize, ids[iIndex] );
    } );
}

GC_API IFUpdateDeviceList( IF_HANDLE hIface, bool8_t * pbChanged, uint64_t /*iTimeout*/ )
{
    return guarded( [&] {
        bool changed = get< interface_module >( hIface )->update_devices();
        if( pbChanged )
            *pbChanged = changed;
    } );
}

GC_API IFGetDeviceInfo( IF_HANDLE hIface, const char * sDeviceID, DEVICE_INFO_CMD iInfoCmd, INFO_DATATYPE * piType,
                        void * pBuffer, size_t * piSize )
{
    return guarded( [&] {
        get< interface_module >( hIface )->device_info( as_string( sDeviceID ), iInfoCmd, piType, pBuffer, piSize );
    } );
}

GC_API IFOpenDevice( IF_HANDLE hIface, const char * sDeviceID, DEVICE_ACCESS_FLAGS iOpenFlags, DEV_HANDLE * phDevice )
{
    return guarded( [&] {
        if( ! phDevice )
            throw gentl_error( GC_ERR_INVALID_PARAMETER, "null handle pointer" );
        if( iOpenFlags == DEVICE_ACCESS_READONLY )
            throw gentl_error( GC_ERR_ACCESS_DENIED, "read-only access is not supported" );
        auto dev = get< interface_module >( hIface )->open_device( as_string( sDeviceID ) );
        auto port = std::make_shared< remote_port >( dev, dev->make_remote_map() );
        registry::instance().add( dev );
        registry::instance().add( port );
        *phDevice = handle( dev );
    } );
}

GC_API IFGetParentTL( IF_HANDLE hIface, TL_HANDLE * phSystem )
{
    return guarded( [&] { set_out( phSystem, static_cast< void * >( get< interface_module >( hIface )->parent() ) ); } );
}

GC_API DevGetPort( DEV_HANDLE hDevice, PORT_HANDLE * phRemoteDevice )
{
    return guarded( [&] {
        auto ports = registry::instance().children< remote_port >( get< device_module >( hDevice ).get() );
        if( ports.empty() )
            throw gentl_error( GC_ERR_NOT_AVAILABLE, "no remote port" );
        set_out( phRemoteDevice, handle( ports.front() ) );
    } );
}

GC_API DevGetNumDataStreams( DEV_HANDLE hDevice, uint32_t * piNumDataStreams )
{
    return guarded( [&] { set_out( piNumDataStreams, uint32_t( get< device_module >( hDevice )->stream_ids().size() ) ); } );
}

GC_API DevGetDataStreamID( DEV_HANDLE hDevice, uint32_t iIndex, char * sDataStreamID, size_t * piSize )
{
    return guarded( [&] {
        auto ids = get< device_module >( hDevice )->stream_ids();
        if( iIndex >= ids.size() )
            throw gentl_error( GC_ERR_INVALID_INDEX, "data stream index out of range" );
        info_string( nullptr, sDataStreamID, piSize, ids[iIndex] );
    } );
}

GC_API DevOpenDataStream( DEV_HANDLE hDevice, const char * sDataStreamID, DS_HANDLE * phDataStream )
{
    return guarded( [&] {
        if( ! phDataStream )
            throw gentl_error( GC_ERR_INVALID_PARAMETER, "null handle pointer" );
        auto dev = get< device_module >( hDevice );
        std::string id = as_string( sDataStreamID );
        auto ds = std::make_shared< data_stream >( dev, dev->component_of( id ), id );
        *phDataStream = handle( registry::instance().add( ds ) );
    } );
}

GC_API DevGetInfo( DEV_HANDLE hDevice, DEVICE_INFO_CMD iInfoCmd, INFO_DATATYPE * piType, void * pBuffer, size_t * piSize )
{
    return guarded( [&] { get< device_module >( hDevice )->info( iInfoCmd, piType, pBuffer, piSize ); } );
}

GC_API DevClose( DEV_HANDLE hDevice )
{
    return guarded( [&] { registry::instance().close( get< device_module >( hDevice ).get() ); } );
}

GC_API DevGetParentIF( DEV_HANDLE hDevice, IF_HANDLE * phIface )
{
    return guarded( [&] { set_out( phIface, static_cast< void * >( get< device_module >( hDevice )->parent() ) ); } );
}

GC_API DSAnnounceBuffer( DS_HANDLE hDataStream, void * pBuffer, size_t iSize, void * pPrivate, BUFFER_HANDLE * phBuffer )
{
    return guarded( [&] {
        set_out( phBuffer, handle( get< data_stream >( hDataStream )->announce( pBuffer, iSize, pPrivate, false ) ) );
    } );
}

GC_API DSAllocAndAnnounceBuffer( DS_HANDLE hDataStream, size_t iSize, void * pPrivate, BUFFER_HANDLE * phBuffer )
{
    return guarded( [&] {
        set_out( phBuffer, handle( get< data_stream >( hDataStream )->announce( nullptr, iSize, pPrivate, true ) ) );
    } );
}

GC_API DSFlushQueue( DS_HANDLE hDataStream, ACQ_QUEUE_TYPE iOperation )
{
    return guarded( [&] { get< data_stream >( hDataStream )->flush( iOperation ); } );
}

GC_API DSStartAcquisition( DS_HANDLE hDataStream, ACQ_START_FLAGS /*iStartFlags*/, uint64_t iNumToAcquire )
{
    return guarded( [&] { get< data_stream >( hDataStream )->start( iNumToAcquire ); } );
}

GC_API DSStopAcquisition( DS_HANDLE hDataStream, ACQ_STOP_FLAGS /*iStopFlags*/ )
{
    return guarded( [&] { get< data_stream >( hDataStream )->stop(); } );
}

GC_API DSGetInfo( DS_HANDLE hDataStream, STREAM_INFO_CMD iInfoCmd, INFO_DATATYPE * piType, void * pBuffer, size_t * piSize )
{
    return guarded( [&] { get< data_stream >( hDataStream )->info( iInfoCmd, piType, pBuffer, piSize ); } );
}

GC_API DSGetBufferID( DS_HANDLE hDataStream, uint32_t iIndex, BUFFER_HANDLE * phBuffer )
{
    return guarded( [&] { set_out( phBuffer, handle( get< data_stream >( hDataStream )->buffer_at( iIndex ) ) ); } );
}

GC_API DSClose( DS_HANDLE hDataStream )
{
    return guarded( [&] { registry::instance().close( get< data_stream >( hDataStream ).get() ); } );
}

GC_API DSRevokeBuffer( DS_HANDLE hDataStream, BUFFER_HANDLE hBuffer, void ** pBuffer, void ** pPrivate )
{
    return guarded( [&] {
        std::shared_ptr< buffer > b;
        stream_of_buffer( hDataStream, hBuffer, b )->revoke( *b, pBuffer, pPrivate );
    } );
}

GC_API DSQueueBuffer( DS_HANDLE hDataStream, BUFFER_HANDLE hBuffer )
{
    return guarded( [&] {
        std::shared_ptr< buffer > b;
        stream_of_buffer( hDataStream, hBuffer, b )->queue( *b );
    } );
}

GC_API DSGetBufferInfo( DS_HANDLE hDataStream, BUFFER_HANDLE hBuffer, BUFFER_INFO_CMD iInfoCmd, INFO_DATATYPE * piType,
                        void * pBuffer, size_t * piSize )
{
    return guarded( [&] {
        std::shared_ptr< buffer > b;
        stream_of_buffer( hDataStream, hBuffer, b )->buffer_info( *b, iInfoCmd, piType, pBuffer, piSize );
    } );
}

GC_API DSGetBufferChunkData( DS_HANDLE hDataStream, BUFFER_HANDLE hBuffer, SINGLE_CHUNK_DATA * /*pChunkData*/,
                             size_t * piNumChunks )
{
    return guarded( [&] {
        std::shared_ptr< buffer > b;
        stream_of_buffer( hDataStream, hBuffer, b );
        set_out( piNumChunks, size_t( 0 ) );
    } );
}

GC_API DSGetParentDev( DS_HANDLE hDataStream, DEV_HANDLE * phDevice )
{
    return guarded( [&] { set_out( phDevice, static_cast< void * >( get< data_stream >( hDataStream )->parent() ) ); } );
}

GC_API DSGetNumBufferParts( DS_HANDLE hDataStream, BUFFER_HANDLE hBuffer, uint32_t * piNumParts )
{
    return guarded( [&] {
        std::shared_ptr< buffer > b;
        stream_of_buffer( hDataStream, hBuffer, b );
        set_out( piNumParts, 0u );
    } );
}

GC_API DSGetBufferPartInfo( DS_HANDLE hDataStream, BUFFER_HANDLE hBuffer, uint32_t /*iPartIndex*/,
                            BUFFER_PART_INFO_CMD /*iInfoCmd*/, INFO_DATATYPE * /*piType*/, void * /*pBuffer*/,
                            size_t * /*piSize*/ )
{
    return guarded( [&] {
        std::shared_ptr< buffer > b;
        stream_of_buffer( hDataStream, hBuffer, b );
        throw gentl_error( GC_ERR_INVALID_INDEX, "buffers are not multi-part" );
    } );
}

}  // namespace GenTL
