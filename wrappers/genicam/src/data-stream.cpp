// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

#include "modules.h"

#include <algorithm>
#include <chrono>

namespace rs2_gentl {

buffer::buffer( module * parent, void * base_, size_t size_, void * user_, bool owned_ )
    : module( parent )
    , base( static_cast< uint8_t * >( base_ ) )
    , size( size_ )
    , user( user_ )
    , owned( owned_ )
{
    if( owned )
    {
        storage.resize( size );
        base = storage.data();
    }
}

data_stream::data_stream( std::shared_ptr< device_module > dev, component_id c, std::string id )
    : module( dev.get() )
    , _device( std::move( dev ) )
    , _component( c )
    , _id( std::move( id ) )
{
    _port.reset( new node_map( "TLPort", "RealSense_GenTL_DataStream", "9B4C2E81-7D3A-4F6E-B5C1-2A8D6E9F0C35" ) );
    auto & map = *_port;
    map.add_string( "StreamID", [this] { return _id; } );
    auto counter = [this]( STREAM_INFO_CMD cmd ) {
        return [this, cmd]( int ) {
            size_t value = 0, size = sizeof( value );
            info( cmd, nullptr, &value, &size );
            return int64_t( value );
        };
    };
    map.add_integer( "StreamAnnouncedBufferCount", counter( STREAM_INFO_NUM_ANNOUNCED ), nullptr );
    map.add_integer( "StreamAnnounceBufferMinimum", []( int ) { return int64_t( 1 ); }, nullptr );
    map.add_category( "StreamInformation", { "StreamID", "StreamAnnouncedBufferCount", "StreamAnnounceBufferMinimum" } );
    map.add_category( "Root", { "StreamInformation" } );
    map.finalize();
    _device->attach( _component, this );
}

void data_stream::on_close()
{
    _device->detach( _component, this );
    std::lock_guard< std::mutex > lock( _mutex );
    _acquiring = false;
    _closed = true;
    _cv.notify_all();
}

void data_stream::info( STREAM_INFO_CMD cmd, INFO_DATATYPE * type, void * out, size_t * size )
{
    // Queried before taking the stream lock: AcquisitionStart takes the device lock, then the stream lock
    if( cmd == STREAM_INFO_PAYLOAD_SIZE )
        return info_sizet( type, out, size, _device->payload_size( _component ) );
    std::lock_guard< std::mutex > lock( _mutex );
    switch( cmd )
    {
    case STREAM_INFO_ID: return info_string( type, out, size, _id );
    case STREAM_INFO_NUM_DELIVERED: return info_u64( type, out, size, _delivered );
    case STREAM_INFO_NUM_UNDERRUN: return info_u64( type, out, size, _underrun );
    case STREAM_INFO_NUM_ANNOUNCED: return info_sizet( type, out, size, _announced.size() );
    case STREAM_INFO_NUM_QUEUED: return info_sizet( type, out, size, _input.size() );
    case STREAM_INFO_NUM_AWAIT_DELIVERY: return info_sizet( type, out, size, _output.size() );
    case STREAM_INFO_NUM_STARTED: return info_u64( type, out, size, _delivered );
    case STREAM_INFO_IS_GRABBING: return info_bool( type, out, size, _acquiring );
    case STREAM_INFO_DEFINES_PAYLOADSIZE: return info_bool( type, out, size, true );
    case STREAM_INFO_TLTYPE: return info_string( type, out, size, TL_TYPE );
    case STREAM_INFO_NUM_CHUNKS_MAX: return info_sizet( type, out, size, 0 );
    case STREAM_INFO_BUF_ANNOUNCE_MIN: return info_sizet( type, out, size, 1 );
    case STREAM_INFO_BUF_ALIGNMENT: return info_sizet( type, out, size, 1 );
    default: unknown_info( cmd );
    }
}

void data_stream::buffer_info( buffer & b, BUFFER_INFO_CMD cmd, INFO_DATATYPE * type, void * out, size_t * size )
{
    std::lock_guard< std::mutex > lock( _mutex );
    if( b.revoked )
        throw gentl_error( GC_ERR_INVALID_HANDLE, "buffer was revoked" );
    switch( cmd )
    {
    case BUFFER_INFO_BASE: return info_ptr( type, out, size, b.base );
    case BUFFER_INFO_SIZE: return info_sizet( type, out, size, b.size );
    case BUFFER_INFO_USER_PTR: return info_ptr( type, out, size, b.user );
    case BUFFER_INFO_TIMESTAMP:
    case BUFFER_INFO_TIMESTAMP_NS: return info_u64( type, out, size, b.timestamp_ns );
    case BUFFER_INFO_NEW_DATA: return info_bool( type, out, size, b.new_data );
    case BUFFER_INFO_IS_QUEUED: return info_bool( type, out, size, b.state != buffer::UNQUEUED );
    case BUFFER_INFO_IS_ACQUIRING: return info_bool( type, out, size, false );
    case BUFFER_INFO_IS_INCOMPLETE: return info_bool( type, out, size, b.incomplete );
    case BUFFER_INFO_TLTYPE: return info_string( type, out, size, TL_TYPE );
    case BUFFER_INFO_SIZE_FILLED:
    case BUFFER_INFO_DATA_SIZE: return info_sizet( type, out, size, b.filled );
    case BUFFER_INFO_WIDTH: return info_sizet( type, out, size, b.width );
    case BUFFER_INFO_HEIGHT:
    case BUFFER_INFO_DELIVERED_IMAGEHEIGHT: return info_sizet( type, out, size, b.height );
    case BUFFER_INFO_XOFFSET:
    case BUFFER_INFO_YOFFSET:
    case BUFFER_INFO_XPADDING:
    case BUFFER_INFO_YPADDING:
    case BUFFER_INFO_IMAGEOFFSET:
    case BUFFER_INFO_DELIVERED_CHUNKPAYLOADSIZE: return info_sizet( type, out, size, 0 );
    case BUFFER_INFO_FRAMEID: return info_u64( type, out, size, b.frame_id );
    case BUFFER_INFO_IMAGEPRESENT: return info_bool( type, out, size, b.filled > 0 );
    case BUFFER_INFO_PAYLOADTYPE: return info_sizet( type, out, size, PAYLOAD_TYPE_IMAGE );
    case BUFFER_INFO_PIXELFORMAT: return info_u64( type, out, size, b.pixel_format );
    case BUFFER_INFO_PIXELFORMAT_NAMESPACE: return info_u64( type, out, size, PIXELFORMAT_NAMESPACE_PFNC_32BIT );
    case BUFFER_INFO_CHUNKLAYOUTID: return info_u64( type, out, size, 0 );
    case BUFFER_INFO_PIXEL_ENDIANNESS: return info_value< int32_t >( INFO_DATATYPE_INT32, type, out, size, PIXELENDIANNESS_LITTLE );
    case BUFFER_INFO_DATA_LARGER_THAN_BUFFER: return info_bool( type, out, size, b.larger_than_buffer );
    case BUFFER_INFO_CONTAINS_CHUNKDATA: return info_bool( type, out, size, false );
    default: unknown_info( cmd );
    }
}

std::shared_ptr< buffer > data_stream::announce( void * base, size_t size, void * user, bool allocate )
{
    if( ! allocate && ! base )
        throw gentl_error( GC_ERR_INVALID_PARAMETER, "buffer is null" );
    if( ! size )
        throw gentl_error( GC_ERR_INVALID_PARAMETER, "buffer size is 0" );
    auto b = registry::instance().add( std::make_shared< buffer >( this, base, size, user, allocate ) );
    std::lock_guard< std::mutex > lock( _mutex );
    _announced.push_back( b.get() );
    return b;
}

std::shared_ptr< buffer > data_stream::buffer_at( uint32_t index )
{
    buffer * b;
    {
        std::lock_guard< std::mutex > lock( _mutex );
        if( index >= _announced.size() )
            throw gentl_error( GC_ERR_INVALID_INDEX, "buffer index out of range" );
        b = _announced[index];
    }
    return registry::instance().get< buffer >( static_cast< module * >( b ) );
}

void data_stream::remove( std::deque< buffer * > & queue, buffer * b )
{
    queue.erase( std::remove( queue.begin(), queue.end(), b ), queue.end() );
}

void data_stream::queue( buffer & b )
{
    std::lock_guard< std::mutex > lock( _mutex );
    if( b.revoked )
        throw gentl_error( GC_ERR_INVALID_HANDLE, "buffer was revoked" );
    if( b.state == buffer::INPUT )
        throw gentl_error( GC_ERR_RESOURCE_IN_USE, "buffer is already queued" );
    if( b.state == buffer::OUTPUT )
        remove( _output, &b );
    b.state = buffer::INPUT;
    b.new_data = false;
    b.filled = 0;
    _input.push_back( &b );
}

void data_stream::revoke( buffer & b, void ** base, void ** user )
{
    std::lock_guard< std::mutex > lock( _mutex );
    if( b.revoked )
        throw gentl_error( GC_ERR_INVALID_HANDLE, "buffer was already revoked" );
    if( base )
        *base = b.owned ? nullptr : b.base;
    if( user )
        *user = b.user;
    remove( _input, &b );
    remove( _output, &b );
    _announced.erase( std::remove( _announced.begin(), _announced.end(), &b ), _announced.end() );
    b.state = buffer::UNQUEUED;
    b.revoked = true;
    std::vector< uint8_t >().swap( b.storage );
}

void data_stream::flush( ACQ_QUEUE_TYPE operation )
{
    std::lock_guard< std::mutex > lock( _mutex );
    auto set_state = [this]( std::deque< buffer * > & queue, buffer::state_t state ) {
        for( auto b : queue )
            b->state = state;
    };
    switch( operation )
    {
    case ACQ_QUEUE_INPUT_TO_OUTPUT:
        set_state( _input, buffer::OUTPUT );
        _output.insert( _output.end(), _input.begin(), _input.end() );
        _input.clear();
        _cv.notify_all();
        break;
    case ACQ_QUEUE_OUTPUT_DISCARD:
        set_state( _output, buffer::UNQUEUED );
        _output.clear();
        break;
    case ACQ_QUEUE_ALL_TO_INPUT:
    case ACQ_QUEUE_UNQUEUED_TO_INPUT:
        if( operation == ACQ_QUEUE_ALL_TO_INPUT )
        {
            set_state( _output, buffer::UNQUEUED );
            _output.clear();
        }
        for( auto b : _announced )
            if( b->state == buffer::UNQUEUED )
            {
                b->state = buffer::INPUT;
                b->new_data = false;
                _input.push_back( b );
            }
        break;
    case ACQ_QUEUE_ALL_DISCARD:
        set_state( _input, buffer::UNQUEUED );
        set_state( _output, buffer::UNQUEUED );
        _input.clear();
        _output.clear();
        break;
    default: throw gentl_error( GC_ERR_INVALID_PARAMETER, "unknown flush operation" );
    }
}

void data_stream::start( uint64_t count )
{
    std::lock_guard< std::mutex > lock( _mutex );
    if( _acquiring )
        throw gentl_error( GC_ERR_RESOURCE_IN_USE, "acquisition already started" );
    _acquiring = true;
    _to_acquire = count;
    _delivered = 0;
    _underrun = 0;
}

void data_stream::stop()
{
    // Not an error when the stream already stopped itself after delivering the requested count
    std::lock_guard< std::mutex > lock( _mutex );
    _acquiring = false;
}

bool data_stream::acquiring()
{
    std::lock_guard< std::mutex > lock( _mutex );
    return _acquiring;
}

void data_stream::deliver( const rs2::video_frame & f, uint64_t pixel_format )
{
    std::lock_guard< std::mutex > lock( _mutex );
    if( ! _acquiring )
        return;
    if( _input.empty() )
    {
        ++_underrun;
        return;
    }
    buffer * b = _input.front();
    _input.pop_front();

    size_t row = size_t( f.get_width() ) * f.get_bytes_per_pixel();
    size_t stride = size_t( f.get_stride_in_bytes() );
    size_t height = size_t( f.get_height() );
    if( ! row || stride < row )
        return;
    size_t rows = std::min( height, b->size / row );
    auto src = static_cast< const uint8_t * >( f.get_data() );
    if( stride == row )
        std::memcpy( b->base, src, rows * row );
    else
        for( size_t y = 0; y < rows; ++y )
            std::memcpy( b->base + y * row, src + y * stride, row );

    b->filled = rows * row;
    b->larger_than_buffer = rows < height;
    b->incomplete = b->larger_than_buffer;
    b->width = size_t( f.get_width() );
    b->height = rows;
    b->pixel_format = pixel_format;
    b->timestamp_ns = uint64_t( f.get_timestamp() * 1e6 );
    b->frame_id = f.get_frame_number();
    b->new_data = true;
    b->state = buffer::OUTPUT;
    _output.push_back( b );
    ++_delivered;
    ++_fired;
    if( _to_acquire != GENTL_INFINITE && _delivered >= _to_acquire )
        _acquiring = false;
    _cv.notify_one();
}

void data_stream::wait( EVENT_NEW_BUFFER_DATA & data, uint64_t timeout_ms )
{
    std::unique_lock< std::mutex > lock( _mutex );
    auto ready = [this] { return ! _output.empty() || _kills > 0 || _closed; };
    if( timeout_ms == GENTL_INFINITE )
        _cv.wait( lock, ready );
    else if( ! _cv.wait_for( lock, std::chrono::milliseconds( timeout_ms ), ready ) )
        throw gentl_error( GC_ERR_TIMEOUT, "timeout waiting for a new buffer" );
    if( _kills > 0 )
    {
        --_kills;
        throw gentl_error( GC_ERR_ABORT, "wait aborted by EventKill" );
    }
    if( _output.empty() )
        throw gentl_error( GC_ERR_ABORT, "data stream closed" );
    buffer * b = _output.front();
    _output.pop_front();
    b->state = buffer::UNQUEUED;
    data.BufferHandle = static_cast< module * >( b );
    data.pUserPointer = b->user;
}

void data_stream::kill()
{
    std::lock_guard< std::mutex > lock( _mutex );
    ++_kills;
    _cv.notify_all();
}

// A kill left over from an unregistered event must not abort the next registration's first wait
void data_stream::reset_kills()
{
    std::lock_guard< std::mutex > lock( _mutex );
    _kills = 0;
}

void data_stream::flush_events()
{
    flush( ACQ_QUEUE_OUTPUT_DISCARD );
}

size_t data_stream::events_in_queue()
{
    std::lock_guard< std::mutex > lock( _mutex );
    return _output.size();
}

uint64_t data_stream::events_fired()
{
    std::lock_guard< std::mutex > lock( _mutex );
    return _fired;
}

}  // namespace rs2_gentl
