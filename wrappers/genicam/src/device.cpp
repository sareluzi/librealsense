// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

#include "modules.h"

#include <algorithm>
#include <cctype>
#include <climits>
#include <cmath>
#include <cstdlib>
#include <tuple>

namespace rs2_gentl {

namespace {

// PFNC pixel format codes
const int64_t PFNC_MONO8 = 0x01080001;
const int64_t PFNC_RGB8 = 0x02180014;
const int64_t PFNC_COORD3D_C16 = 0x011000B8;

struct component_desc
{
    const char * name;  // SFNC ComponentSelector value, also the data stream ID
    rs2_stream stream;
    int index;
    rs2_format format;
    int64_t pixel_format;
    int bytes_per_pixel;
    int default_width;
    int default_height;
};

const component_desc COMPONENTS[COMPONENT_COUNT] = {
    { "Range", RS2_STREAM_DEPTH, 0, RS2_FORMAT_Z16, PFNC_COORD3D_C16, 2, 848, 480 },
    { "Infrared", RS2_STREAM_INFRARED, 1, RS2_FORMAT_Y8, PFNC_MONO8, 1, 848, 480 },
    { "Intensity", RS2_STREAM_COLOR, 0, RS2_FORMAT_RGB8, PFNC_RGB8, 3, 1280, 720 },
};

const int DEFAULT_FPS = 30;

// SFNC ExposureTime is in us; the color sensor exposure option is in 100 us units
double exposure_scale( int c )
{
    return c == INTENSITY ? 100. : 1.;
}

}  // namespace

device_module::device_module( interface_module * parent, rs2::device dev )
    : module( parent )
    , _dev( dev )
    , _serial( dev.get_info( RS2_CAMERA_INFO_SERIAL_NUMBER ) )
    , _name( dev.get_info( RS2_CAMERA_INFO_NAME ) )
{
    for( auto && s : _dev.query_sensors() )
    {
        auto profiles = s.get_stream_profiles();
        for( int c = 0; c < COMPONENT_COUNT; ++c )
        {
            auto & d = COMPONENTS[c];
            for( auto && p : profiles )
                if( p.stream_type() == d.stream && p.stream_index() == d.index && p.format() == d.format
                    && p.is< rs2::video_stream_profile >() )
                {
                    _components[c].sensor = s;
                    _components[c].profiles.push_back( p.as< rs2::video_stream_profile >() );
                }
        }
    }
    for( int c = 0; c < COMPONENT_COUNT; ++c )
    {
        auto & comp = _components[c];
        std::sort( comp.profiles.begin(), comp.profiles.end(),
                   []( const rs2::video_stream_profile & a, const rs2::video_stream_profile & b ) {
                       return std::make_tuple( a.width(), a.height(), a.fps() )
                            < std::make_tuple( b.width(), b.height(), b.fps() );
                   } );
        for( size_t i = 0; i < comp.profiles.size(); ++i )
        {
            auto & p = comp.profiles[i];
            if( p.width() == COMPONENTS[c].default_width && p.height() == COMPONENTS[c].default_height
                && p.fps() == DEFAULT_FPS )
                comp.selected = i;
        }
    }
    _components[RANGE].enabled = true;
    _components[INTENSITY].enabled = true;
}

void device_module::identity_info( rs2::device dev, DEVICE_INFO_CMD cmd, INFO_DATATYPE * type, void * buffer,
                                   size_t * size )
{
    auto get = [&]( rs2_camera_info i ) {
        return dev.supports( i ) ? std::string( dev.get_info( i ) ) : std::string();
    };
    switch( cmd )
    {
    case DEVICE_INFO_ID:
    case DEVICE_INFO_SERIAL_NUMBER: return info_string( type, buffer, size, get( RS2_CAMERA_INFO_SERIAL_NUMBER ) );
    case DEVICE_INFO_VENDOR: return info_string( type, buffer, size, VENDOR_NAME );
    case DEVICE_INFO_MODEL: return info_string( type, buffer, size, get( RS2_CAMERA_INFO_NAME ) );
    case DEVICE_INFO_TLTYPE: return info_string( type, buffer, size, TL_TYPE );
    case DEVICE_INFO_DISPLAYNAME:
        return info_string( type, buffer, size,
                            get( RS2_CAMERA_INFO_NAME ) + " (" + get( RS2_CAMERA_INFO_SERIAL_NUMBER ) + ")" );
    case DEVICE_INFO_USER_DEFINED_NAME: return info_string( type, buffer, size, "" );
    case DEVICE_INFO_VERSION: return info_string( type, buffer, size, get( RS2_CAMERA_INFO_FIRMWARE_VERSION ) );
    case DEVICE_INFO_TIMESTAMP_FREQUENCY: return info_u64( type, buffer, size, TIMESTAMP_FREQUENCY );
    default: unknown_info( cmd );
    }
}

void device_module::info( DEVICE_INFO_CMD cmd, INFO_DATATYPE * type, void * buffer, size_t * size ) const
{
    if( cmd == DEVICE_INFO_ACCESS_STATUS )
        return info_value< int32_t >( INFO_DATATYPE_INT32, type, buffer, size, DEVICE_ACCESS_STATUS_OPEN_READWRITE );
    identity_info( _dev, cmd, type, buffer, size );
}

std::vector< std::string > device_module::stream_ids() const
{
    std::vector< std::string > ids;
    for( int c = 0; c < COMPONENT_COUNT; ++c )
        if( _components[c].available() )
            ids.push_back( COMPONENTS[c].name );
    return ids;
}

component_id device_module::component_of( const std::string & stream_id ) const
{
    for( int c = 0; c < COMPONENT_COUNT; ++c )
        if( _components[c].available() && stream_id == COMPONENTS[c].name )
            return component_id( c );
    throw gentl_error( GC_ERR_INVALID_ID, "unknown data stream ID '" + stream_id + "'" );
}

device_module::component & device_module::get( int c )
{
    return const_cast< component & >( static_cast< const device_module * >( this )->get( c ) );
}

const device_module::component & device_module::get( int c ) const
{
    if( c < 0 || c >= COMPONENT_COUNT )
        throw gentl_error( GC_ERR_INVALID_INDEX, "invalid component index " + std::to_string( c ) );
    if( ! _components[c].available() )
        throw gentl_error( GC_ERR_NOT_AVAILABLE, std::string( COMPONENTS[c].name ) + " is not available on this device" );
    return _components[c];
}

size_t device_module::payload_size( component_id c ) const
{
    std::lock_guard< std::recursive_mutex > lock( _mutex );
    auto & p = get( c ).profile();
    return size_t( p.width() ) * p.height() * COMPONENTS[c].bytes_per_pixel;
}

void device_module::check_unlocked() const
{
    if( _streaming || _params_locked )
        throw gentl_error( GC_ERR_ACCESS_DENIED, "stream parameters are locked while acquiring or TLParamsLocked is set" );
}

// Picks the profile that keeps the fixed field (0 = width, 1 = height) and is closest in the others
void device_module::select_profile( int c, int width, int height, int fps, int fixed )
{
    check_unlocked();
    auto & comp = get( c );
    size_t best = comp.profiles.size();
    long best_score = LONG_MAX;
    for( size_t i = 0; i < comp.profiles.size(); ++i )
    {
        auto & p = comp.profiles[i];
        if( ( fixed == 0 && p.width() != width ) || ( fixed == 1 && p.height() != height ) )
            continue;
        long score = std::labs( long( p.width() ) - width ) + std::labs( long( p.height() ) - height )
                   + 1000 * std::labs( long( p.fps() ) - fps );
        if( score < best_score )
        {
            best = i;
            best_score = score;
        }
    }
    if( best == comp.profiles.size() )
        throw gentl_error( GC_ERR_INVALID_VALUE, std::string( "no " ) + COMPONENTS[c].name + " profile with "
                                                     + ( fixed == 0 ? "Width " + std::to_string( width )
                                                                    : "Height " + std::to_string( height ) ) );
    comp.selected = best;
}

void device_module::set_frame_rate( double fps )
{
    check_unlocked();
    for( auto & comp : _components )
    {
        if( ! comp.available() )
            continue;
        auto current = comp.profile();
        double best_diff = 1e9;
        for( size_t i = 0; i < comp.profiles.size(); ++i )
        {
            auto & p = comp.profiles[i];
            double diff = std::fabs( p.fps() - fps );
            if( p.width() == current.width() && p.height() == current.height() && diff < best_diff )
            {
                comp.selected = i;
                best_diff = diff;
            }
        }
    }
}

double device_module::option( int c, rs2_option opt, double scale ) const
{
    auto & s = get( c ).sensor;
    if( ! s.supports( opt ) )
        throw gentl_error( GC_ERR_NOT_AVAILABLE, std::string( rs2_option_to_string( opt ) ) + " is not supported" );
    return s.get_option( opt ) * scale;
}

rs2::option_range device_module::option_range( int c, rs2_option opt ) const
{
    auto & s = get( c ).sensor;
    if( ! s.supports( opt ) )
        throw gentl_error( GC_ERR_NOT_AVAILABLE, std::string( rs2_option_to_string( opt ) ) + " is not supported" );
    return s.get_option_range( opt );
}

void device_module::set_option( int c, rs2_option opt, double value )
{
    // GenApi floats are continuous; rs2 options only accept values on their step grid
    auto range = option_range( c, opt );
    if( range.step > 0 )
        value = range.min + std::round( ( value - range.min ) / range.step ) * range.step;
    get( c ).sensor.set_option( opt, float( std::min( std::max( value, double( range.min ) ), double( range.max ) ) ) );
}

void device_module::attach( component_id c, data_stream * stream )
{
    std::lock_guard< std::mutex > lock( _streams_mutex );
    if( _streams[c] )
        throw gentl_error( GC_ERR_RESOURCE_IN_USE, std::string( "data stream " ) + COMPONENTS[c].name + " is already open" );
    _streams[c] = stream;
}

void device_module::detach( component_id c, data_stream * stream )
{
    std::lock_guard< std::mutex > lock( _streams_mutex );
    if( _streams[c] == stream )
        _streams[c] = nullptr;
}

void device_module::start()
{
    std::lock_guard< std::recursive_mutex > lock( _mutex );
    if( _streaming )
        return;
    std::array< bool, COMPONENT_COUNT > active{};
    {
        std::lock_guard< std::mutex > streams_lock( _streams_mutex );
        for( int c = 0; c < COMPONENT_COUNT; ++c )
            active[c] = _components[c].available()
                     && ( _components[c].enabled || ( _streams[c] && _streams[c]->acquiring() ) );
    }

    // Depth and IR come from one sensor and must share resolution and frame rate
    if( active[RANGE] && active[INFRARED] )
    {
        auto depth = _components[RANGE].profile();
        auto & ir = _components[INFRARED];
        auto it = std::find_if( ir.profiles.begin(), ir.profiles.end(), [&]( const rs2::video_stream_profile & p ) {
            return p.width() == depth.width() && p.height() == depth.height() && p.fps() == depth.fps();
        } );
        if( it == ir.profiles.end() )
            throw gentl_error( GC_ERR_INVALID_VALUE, "no Infrared profile matches the Range resolution and frame rate" );
        ir.selected = it - ir.profiles.begin();
    }

    std::vector< std::pair< rs2::sensor, std::vector< rs2::stream_profile > > > per_sensor;
    for( int c = 0; c < COMPONENT_COUNT; ++c )
    {
        if( ! active[c] )
            continue;
        auto & comp = _components[c];
        auto it = std::find_if( per_sensor.begin(), per_sensor.end(), [&]( const std::pair< rs2::sensor, std::vector< rs2::stream_profile > > & x ) {
            return x.first.get().get() == comp.sensor.get().get();
        } );
        if( it == per_sensor.end() )
            it = per_sensor.insert( per_sensor.end(), { comp.sensor, {} } );
        it->second.push_back( comp.profile() );
    }
    if( per_sensor.empty() )
        throw gentl_error( GC_ERR_ERROR, "no component is enabled" );

    try
    {
        for( auto & x : per_sensor )
        {
            _started.push_back( x.first );
            x.first.open( x.second );
            x.first.start( [this]( rs2::frame f ) { on_frame( f ); } );
        }
    }
    catch( const rs2::error & e )
    {
        stop();
        throw gentl_error( e.get_type() == RS2_EXCEPTION_TYPE_BACKEND ? GC_ERR_RESOURCE_IN_USE : GC_ERR_IO,
                           std::string( "failed to start streaming: " ) + e.what()
                               + " (is another application using the camera?)" );
    }
    _streaming = true;
}

void device_module::stop()
{
    std::lock_guard< std::recursive_mutex > lock( _mutex );
    for( auto & s : _started )
    {
        // A sensor that failed to open or start throws here; nothing else to undo for it
        try { s.stop(); } catch( const rs2::error & ) {}
        try { s.close(); } catch( const rs2::error & ) {}
    }
    _started.clear();
    _streaming = false;
}

void device_module::on_close()
{
    stop();
}

void device_module::on_frame( const rs2::frame & f )
{
    auto p = f.get_profile();
    auto vf = f.as< rs2::video_frame >();
    for( int c = 0; c < COMPONENT_COUNT; ++c )
    {
        if( p.stream_type() != COMPONENTS[c].stream || p.stream_index() != COMPONENTS[c].index || ! vf )
            continue;
        std::lock_guard< std::mutex > lock( _streams_mutex );
        if( _streams[c] )
            _streams[c]->deliver( vf, COMPONENTS[c].pixel_format );
    }
}

std::unique_ptr< node_map > device_module::make_remote_map()
{
    std::string model;
    for( char ch : _name )
        model += std::isalnum( static_cast< unsigned char >( ch ) ) ? ch : '_';
    std::unique_ptr< node_map > map( new node_map( "Device", model, "C2D7E5A4-1F3B-4E6A-8D9C-5B7A3E1F2D44" ) );
    auto & m = *map;
    using lock_t = std::lock_guard< std::recursive_mutex >;
    auto constant = []( int64_t v ) { return [v]( int ) { return v; }; };
    auto constant_f = []( double v ) { return [v]( int ) { return v; }; };
    auto camera_info = [this]( rs2_camera_info i ) {
        std::string value = _dev.supports( i ) ? _dev.get_info( i ) : "";
        return [value] { return value; };
    };
    feature_options per_component;
    per_component.selector = "ComponentSelector";
    per_component.count = COMPONENT_COUNT;
    feature_options custom;
    custom.standard = false;
    std::vector< std::string > root;

    m.add_string( "DeviceVendorName", [] { return std::string( VENDOR_NAME ); } );
    m.add_string( "DeviceModelName", camera_info( RS2_CAMERA_INFO_NAME ) );
    m.add_string( "DeviceSerialNumber", camera_info( RS2_CAMERA_INFO_SERIAL_NUMBER ) );
    m.add_string( "DeviceFirmwareVersion", camera_info( RS2_CAMERA_INFO_FIRMWARE_VERSION ) );
    m.add_enumeration( "DeviceScanType", { { "Areascan3D", 0 } }, constant( 0 ), nullptr );
    m.add_category( "DeviceControl", { "DeviceVendorName", "DeviceModelName", "DeviceSerialNumber",
                                       "DeviceFirmwareVersion", "DeviceScanType" } );
    root.push_back( "DeviceControl" );

    node_map::enum_entries components;
    for( int c = 0; c < COMPONENT_COUNT; ++c )
        if( _components[c].available() )
            components.emplace_back( COMPONENTS[c].name, c );
    m.add_enumeration(
        "ComponentSelector", components,
        [this]( int ) {
            lock_t lock( _mutex );
            return int64_t( _selector );
        },
        [this]( int, int64_t v ) {
            lock_t lock( _mutex );
            get( int( v ) );
            _selector = int( v );
        } );
    m.add_boolean(
        "ComponentEnable",
        [this]( int c ) {
            lock_t lock( _mutex );
            return int64_t( get( c ).enabled );
        },
        [this]( int c, int64_t v ) {
            lock_t lock( _mutex );
            check_unlocked();
            get( c ).enabled = v != 0;
        },
        per_component );
    m.add_enumeration( "PixelFormat",
                       { { "Mono8", PFNC_MONO8 }, { "RGB8", PFNC_RGB8 }, { "Coord3D_C16", PFNC_COORD3D_C16 } },
                       [this]( int c ) {
                           get( c );
                           return COMPONENTS[c].pixel_format;
                       },
                       nullptr, per_component );
    auto dimension = [&]( const char * name, int fixed ) {
        auto value = [fixed]( const rs2::video_stream_profile & p ) { return int64_t( fixed == 0 ? p.width() : p.height() ); };
        auto bound = [this, value]( bool is_max ) {
            return [this, value, is_max]( int c ) {
                lock_t lock( _mutex );
                int64_t result = is_max ? 0 : INT64_MAX;
                for( auto & p : get( c ).profiles )
                    result = is_max ? std::max( result, value( p ) ) : std::min( result, value( p ) );
                return result;
            };
        };
        m.add_integer(
            name,
            [this, value]( int c ) {
                lock_t lock( _mutex );
                return value( get( c ).profile() );
            },
            [this, fixed]( int c, int64_t v ) {
                lock_t lock( _mutex );
                auto p = get( c ).profile();
                select_profile( c, fixed == 0 ? int( v ) : p.width(), fixed == 1 ? int( v ) : p.height(), p.fps(), fixed );
            },
            per_component, bound( false ), bound( true ) );
    };
    dimension( "Width", 0 );
    dimension( "Height", 1 );
    m.add_category( "ImageFormatControl", { "ComponentSelector", "ComponentEnable", "PixelFormat", "Width", "Height" } );
    root.push_back( "ImageFormatControl" );

    auto main_component = [this]() -> const component & {
        for( int c : { RANGE, INTENSITY, INFRARED } )
            if( _components[c].available() )
                return _components[c];
        throw gentl_error( GC_ERR_NOT_AVAILABLE, "device has no video streams" );
    };
    auto fps_bound = [this]( bool is_max ) {
        return [this, is_max]( int ) {
            lock_t lock( _mutex );
            double result = is_max ? 0 : 1e9;
            for( auto & comp : _components )
                for( auto & p : comp.profiles )
                    result = is_max ? std::max( result, double( p.fps() ) ) : std::min( result, double( p.fps() ) );
            return result;
        };
    };
    feature_options hz;
    hz.unit = "Hz";
    m.add_enumeration( "AcquisitionMode", { { "Continuous", 0 } }, constant( 0 ), []( int, int64_t v ) {
        if( v != 0 )
            throw gentl_error( GC_ERR_INVALID_VALUE, "only Continuous acquisition is supported" );
    } );
    m.add_command( "AcquisitionStart", [this] { start(); } );
    m.add_command( "AcquisitionStop", [this] { stop(); } );
    m.add_float(
        "AcquisitionFrameRate",
        [this, main_component]( int ) {
            lock_t lock( _mutex );
            return double( main_component().profile().fps() );
        },
        [this]( int, double v ) {
            lock_t lock( _mutex );
            set_frame_rate( v );
        },
        hz, fps_bound( false ), fps_bound( true ) );
    feature_options exposure = per_component;
    exposure.unit = "us";
    m.add_float(
        "ExposureTime", [this]( int c ) { return option( c, RS2_OPTION_EXPOSURE, exposure_scale( c ) ); },
        [this]( int c, double v ) { set_option( c, RS2_OPTION_EXPOSURE, v / exposure_scale( c ) ); }, exposure,
        [this]( int c ) { return option_range( c, RS2_OPTION_EXPOSURE ).min * exposure_scale( c ); },
        [this]( int c ) { return option_range( c, RS2_OPTION_EXPOSURE ).max * exposure_scale( c ); } );
    m.add_enumeration(
        "ExposureAuto", { { "Off", 0 }, { "Continuous", 2 } },
        [this]( int c ) { return int64_t( option( c, RS2_OPTION_ENABLE_AUTO_EXPOSURE, 1 ) ? 2 : 0 ); },
        [this]( int c, int64_t v ) { set_option( c, RS2_OPTION_ENABLE_AUTO_EXPOSURE, v ? 1 : 0 ); }, per_component );
    m.add_category( "AcquisitionControl", { "AcquisitionMode", "AcquisitionStart", "AcquisitionStop",
                                            "AcquisitionFrameRate", "ExposureTime", "ExposureAuto" } );
    root.push_back( "AcquisitionControl" );

    m.add_float(
        "Gain", [this]( int c ) { return option( c, RS2_OPTION_GAIN, 1 ); },
        [this]( int c, double v ) { set_option( c, RS2_OPTION_GAIN, v ); }, per_component,
        [this]( int c ) { return double( option_range( c, RS2_OPTION_GAIN ).min ); },
        [this]( int c ) { return double( option_range( c, RS2_OPTION_GAIN ).max ); } );
    m.add_category( "AnalogControl", { "Gain" } );
    root.push_back( "AnalogControl" );

    if( _components[RANGE].available() )
    {
        auto intrinsic = [this]( float rs2_intrinsics::*field ) {
            return [this, field]( int ) {
                lock_t lock( _mutex );
                return double( get( RANGE ).profile().get_intrinsics().*field );
            };
        };
        feature_options pixel;
        pixel.unit = "Pixel";
        m.add_enumeration( "Scan3dDistanceUnit", { { "Millimeter", 0 } }, constant( 0 ), nullptr );
        m.add_enumeration( "Scan3dOutputMode", { { "ProjectedC", 0 } }, constant( 0 ), nullptr );
        m.add_enumeration( "Scan3dCoordinateSelector", { { "CoordinateC", 2 } }, constant( 2 ), []( int, int64_t v ) {
            if( v != 2 )
                throw gentl_error( GC_ERR_INVALID_VALUE, "only CoordinateC is available" );
        } );
        m.add_float( "Scan3dCoordinateScale", [this]( int ) { return option( RANGE, RS2_OPTION_DEPTH_UNITS, 1000. ); },
                     nullptr );
        m.add_float( "Scan3dCoordinateOffset", constant_f( 0 ), nullptr );
        m.add_boolean( "Scan3dInvalidDataFlag", constant( 1 ), nullptr );
        m.add_float( "Scan3dInvalidDataValue", constant_f( 0 ), nullptr );
        m.add_float( "Scan3dFocalLength", intrinsic( &rs2_intrinsics::fx ), nullptr, pixel );
        m.add_float( "Scan3dPrincipalPointU", intrinsic( &rs2_intrinsics::ppx ), nullptr, pixel );
        m.add_float( "Scan3dPrincipalPointV", intrinsic( &rs2_intrinsics::ppy ), nullptr, pixel );
        std::vector< std::string > scan3d = { "Scan3dDistanceUnit", "Scan3dOutputMode", "Scan3dCoordinateSelector",
                                              "Scan3dCoordinateScale", "Scan3dCoordinateOffset", "Scan3dInvalidDataFlag",
                                              "Scan3dInvalidDataValue", "Scan3dFocalLength", "Scan3dPrincipalPointU",
                                              "Scan3dPrincipalPointV" };
        if( _components[RANGE].sensor.supports( RS2_OPTION_STEREO_BASELINE ) )
        {
            feature_options mm;
            mm.unit = "mm";
            m.add_float( "Scan3dBaseline", [this]( int ) { return option( RANGE, RS2_OPTION_STEREO_BASELINE, 1 ); },
                         nullptr, mm );
            scan3d.push_back( "Scan3dBaseline" );
        }
        m.add_category( "Scan3dControl", scan3d );
        root.push_back( "Scan3dControl" );
    }

    m.add_integer( "PayloadSize", [this]( int c ) { return int64_t( payload_size( component_id( c ) ) ); }, nullptr,
                   per_component );
    m.add_integer(
        "TLParamsLocked",
        [this]( int ) {
            lock_t lock( _mutex );
            return _params_locked;
        },
        [this]( int, int64_t v ) {
            lock_t lock( _mutex );
            _params_locked = v;
        },
        {}, constant( 0 ), constant( 1 ) );
    m.add_category( "TransportLayerControl", { "PayloadSize", "TLParamsLocked" } );
    root.push_back( "TransportLayerControl" );

    std::vector< std::string > vendor;
    auto & depth_sensor = _components[RANGE].sensor;
    if( _components[RANGE].available() && depth_sensor.supports( RS2_OPTION_LASER_POWER ) )
    {
        feature_options mw = custom;
        mw.unit = "mW";
        m.add_float(
            "RsLaserPower", [this]( int ) { return option( RANGE, RS2_OPTION_LASER_POWER, 1 ); },
            [this]( int, double v ) { set_option( RANGE, RS2_OPTION_LASER_POWER, v ); }, mw,
            [this]( int ) { return double( option_range( RANGE, RS2_OPTION_LASER_POWER ).min ); },
            [this]( int ) { return double( option_range( RANGE, RS2_OPTION_LASER_POWER ).max ); } );
        vendor.push_back( "RsLaserPower" );
    }
    if( _components[RANGE].available() && depth_sensor.supports( RS2_OPTION_EMITTER_ENABLED ) )
    {
        m.add_integer(
            "RsEmitterEnabled", [this]( int ) { return int64_t( option( RANGE, RS2_OPTION_EMITTER_ENABLED, 1 ) ); },
            [this]( int, int64_t v ) { set_option( RANGE, RS2_OPTION_EMITTER_ENABLED, double( v ) ); }, custom,
            [this]( int ) { return int64_t( option_range( RANGE, RS2_OPTION_EMITTER_ENABLED ).min ); },
            [this]( int ) { return int64_t( option_range( RANGE, RS2_OPTION_EMITTER_ENABLED ).max ); } );
        vendor.push_back( "RsEmitterEnabled" );
    }
    if( ! vendor.empty() )
    {
        m.add_category( "RealSenseControl", vendor, false );
        root.push_back( "RealSenseControl" );
    }

    m.add_category( "Root", root );
    m.finalize();
    return map;
}

}  // namespace rs2_gentl
