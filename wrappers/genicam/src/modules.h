// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

#pragma once

#include "gentl-common.h"
#include "node-map.h"

#include <librealsense2/rs.hpp>

#include <array>
#include <condition_variable>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <vector>

namespace rs2_gentl {

// Base of every object handed out as a GenTL handle. Modules that own a node map also act as GenTL ports.
class module
{
public:
    virtual ~module() = default;
    module * parent() const { return _parent; }
    node_map * port() const { return _port.get(); }
    virtual const char * module_name() const = 0;
    virtual std::string id() const = 0;
    virtual void on_close() {}

protected:
    explicit module( module * parent )
        : _parent( parent )
    {
    }
    std::unique_ptr< node_map > _port;

private:
    module * _parent;
};

// Owns every live handle; closing a handle closes its children first
class registry
{
public:
    static registry & instance();

    template< class T >
    std::shared_ptr< T > add( std::shared_ptr< T > m )
    {
        std::lock_guard< std::mutex > lock( _mutex );
        _handles[m.get()] = m;
        return m;
    }

    template< class T >
    std::shared_ptr< T > get( const void * handle )
    {
        std::lock_guard< std::mutex > lock( _mutex );
        auto it = _handles.find( static_cast< module * >( const_cast< void * >( handle ) ) );
        if( it != _handles.end() )
            if( auto typed = std::dynamic_pointer_cast< T >( it->second ) )
                return typed;
        throw gentl_error( GC_ERR_INVALID_HANDLE, "invalid handle" );
    }

    template< class T >
    std::vector< std::shared_ptr< T > > children( const module * parent )
    {
        std::lock_guard< std::mutex > lock( _mutex );
        std::vector< std::shared_ptr< T > > result;
        for( auto & h : _handles )
            if( h.first->parent() == parent )
                if( auto typed = std::dynamic_pointer_cast< T >( h.second ) )
                    result.push_back( typed );
        return result;
    }

    void close( module * m );
    void clear();

private:
    void collect( module * m, std::vector< std::shared_ptr< module > > & order );

    std::mutex _mutex;
    std::map< module *, std::shared_ptr< module > > _handles;
};

const char * const TL_ID = "RealSenseGenTL";

class system_module : public module
{
public:
    system_module();
    const char * module_name() const override { return TLSystemModuleName; }
    std::string id() const override { return TL_ID; }
    static void info( TL_INFO_CMD cmd, INFO_DATATYPE * type, void * buffer, size_t * size );
    rs2::context & context() { return _ctx; }

private:
    rs2::context _ctx;
};

const char * const INTERFACE_ID = "RealSense";

class device_module;

class interface_module : public module
{
public:
    interface_module( system_module * parent );
    const char * module_name() const override { return TLInterfaceModuleName; }
    std::string id() const override { return INTERFACE_ID; }
    static void info( INTERFACE_INFO_CMD cmd, INFO_DATATYPE * type, void * buffer, size_t * size );

    bool update_devices();
    std::vector< std::string > device_ids();
    void device_info( const std::string & id, DEVICE_INFO_CMD cmd, INFO_DATATYPE * type, void * buffer, size_t * size );
    std::shared_ptr< device_module > open_device( const std::string & id );

private:
    rs2::device find( const std::string & id );

    std::mutex _mutex;
    std::vector< std::pair< std::string, rs2::device > > _devices;  // by serial number
};

enum component_id
{
    RANGE,
    INFRARED,
    INTENSITY,
    COMPONENT_COUNT
};

class data_stream;

class device_module : public module
{
public:
    device_module( interface_module * parent, rs2::device dev );
    const char * module_name() const override { return TLDeviceModuleName; }
    std::string id() const override { return _serial; }
    void on_close() override;

    void info( DEVICE_INFO_CMD cmd, INFO_DATATYPE * type, void * buffer, size_t * size ) const;
    // Identity fields, shared with devices that are listed but not open
    static void identity_info( rs2::device dev, DEVICE_INFO_CMD cmd, INFO_DATATYPE * type, void * buffer, size_t * size );
    std::vector< std::string > stream_ids() const;
    component_id component_of( const std::string & stream_id ) const;
    std::unique_ptr< node_map > make_remote_map();

    void attach( component_id c, data_stream * stream );
    void detach( component_id c, data_stream * stream );
    size_t payload_size( component_id c ) const;

private:
    struct component
    {
        rs2::sensor sensor;
        std::vector< rs2::video_stream_profile > profiles;
        size_t selected = 0;
        bool enabled = false;
        bool available() const { return ! profiles.empty(); }
        const rs2::video_stream_profile & profile() const { return profiles[selected]; }
    };

    component & get( int c );
    const component & get( int c ) const;
    void check_unlocked() const;
    void select_profile( int c, int width, int height, int fps, int fixed );
    void set_frame_rate( double fps );
    double option( int c, rs2_option opt, double scale ) const;
    void set_option( int c, rs2_option opt, double value );
    rs2::option_range option_range( int c, rs2_option opt ) const;
    void start();
    void stop();
    void on_frame( const rs2::frame & f );

    rs2::device _dev;
    std::string _serial;
    std::string _name;
    std::array< component, COMPONENT_COUNT > _components;
    int _selector = RANGE;
    bool _streaming = false;
    int64_t _params_locked = 0;
    std::vector< rs2::sensor > _started;
    mutable std::recursive_mutex _mutex;  // features and start/stop
    std::mutex _streams_mutex;            // attached streams, also taken by the frame callback
    std::array< data_stream *, COMPONENT_COUNT > _streams{};
};

// Serves the remote device node map; a separate handle from the device module per GenTL
class remote_port : public module
{
public:
    remote_port( std::shared_ptr< device_module > dev, std::unique_ptr< node_map > map )
        : module( dev.get() )
        , _device( std::move( dev ) )  // the node map calls into the device
    {
        _port = std::move( map );
    }
    const char * module_name() const override { return TLRemoteDeviceModuleName; }
    std::string id() const override { return parent()->id() + "-remote"; }

private:
    std::shared_ptr< device_module > _device;
};

class buffer : public module
{
public:
    enum state_t
    {
        UNQUEUED,
        INPUT,
        OUTPUT
    };

    buffer( module * parent, void * base, size_t size, void * user, bool owned );
    const char * module_name() const override { return TLBufferModuleName; }
    std::string id() const override { return "buffer"; }

    uint8_t * base;
    size_t size;
    void * user;
    bool owned;                      // the producer allocated the memory
    std::vector< uint8_t > storage;
    state_t state = UNQUEUED;
    bool revoked = false;  // kept until the stream closes, so a stale handle is reported instead of reused
    bool new_data = false;
    bool incomplete = false;
    bool larger_than_buffer = false;
    size_t filled = 0;
    size_t width = 0, height = 0;
    uint64_t pixel_format = 0;
    uint64_t timestamp_ns = 0;
    uint64_t frame_id = 0;
};

class data_stream : public module
{
public:
    data_stream( std::shared_ptr< device_module > dev, component_id c, std::string id );
    const char * module_name() const override { return TLDataStreamModuleName; }
    std::string id() const override { return _id; }
    void on_close() override;

    void info( STREAM_INFO_CMD cmd, INFO_DATATYPE * type, void * buffer, size_t * size );
    void buffer_info( buffer & b, BUFFER_INFO_CMD cmd, INFO_DATATYPE * type, void * out, size_t * size );

    std::shared_ptr< buffer > announce( void * base, size_t size, void * user, bool allocate );
    std::shared_ptr< buffer > buffer_at( uint32_t index );
    void queue( buffer & b );
    void revoke( buffer & b, void ** base, void ** user );
    void flush( ACQ_QUEUE_TYPE operation );
    void start( uint64_t count );
    void stop();
    bool acquiring();
    void deliver( const rs2::video_frame & f, uint64_t pixel_format );

    // NEW_BUFFER event; the output queue doubles as the event data queue
    void wait( EVENT_NEW_BUFFER_DATA & data, uint64_t timeout_ms );
    void kill();
    void reset_kills();
    void flush_events();
    size_t events_in_queue();
    uint64_t events_fired();

private:
    void remove( std::deque< buffer * > & queue, buffer * b );

    std::shared_ptr< device_module > _device;
    component_id _component;
    std::string _id;
    std::mutex _mutex;
    std::condition_variable _cv;
    std::vector< buffer * > _announced;
    std::deque< buffer * > _input;
    std::deque< buffer * > _output;
    bool _acquiring = false;
    bool _closed = false;
    uint64_t _to_acquire = 0;
    uint64_t _delivered = 0;
    uint64_t _delivered_total = 0;
    uint64_t _underrun = 0;
    uint64_t _fired = 0;
    int _kills = 0;
};

class event : public module
{
public:
    explicit event( std::shared_ptr< data_stream > s )
        : module( s.get() )
        , stream( std::move( s ) )
    {
    }
    const char * module_name() const override { return "Event"; }
    std::string id() const override { return "NewBuffer"; }

    std::shared_ptr< data_stream > stream;
};

}  // namespace rs2_gentl
