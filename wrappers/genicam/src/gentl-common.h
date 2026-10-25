// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

#pragma once

#include <GenTL.h>

#include <cstring>
#include <stdexcept>
#include <string>

namespace rs2_gentl {

using namespace GenTL;

const char * const VENDOR_NAME = "RealSense";
const char * const TL_TYPE = TLTypeCustomName;
const uint64_t TIMESTAMP_FREQUENCY = 1000000000ULL;  // buffer timestamps are in ns

// Carries a GenTL error code up to the exported C function, which stores it for GCGetLastError
class gentl_error : public std::runtime_error
{
public:
    gentl_error( GC_ERROR code, const std::string & message )
        : std::runtime_error( message )
        , _code( code )
    {
    }
    GC_ERROR code() const { return _code; }

private:
    GC_ERROR _code;
};

// GenTL info calls share one contract: a null buffer queries the required size, a short buffer is an error
inline void check_info_size( void * buffer, size_t * size, size_t required )
{
    if( ! size )
        throw gentl_error( GC_ERR_INVALID_PARAMETER, "size pointer is null" );
    if( buffer && *size < required )
        throw gentl_error( GC_ERR_BUFFER_TOO_SMALL, "buffer too small, " + std::to_string( required ) + " bytes needed" );
}

inline void info_string( INFO_DATATYPE * type, void * buffer, size_t * size, const std::string & value )
{
    check_info_size( buffer, size, value.size() + 1 );
    if( type )
        *type = INFO_DATATYPE_STRING;
    if( buffer )
        std::memcpy( buffer, value.c_str(), value.size() + 1 );
    *size = value.size() + 1;
}

template< class T >
void info_value( INFO_DATATYPE data_type, INFO_DATATYPE * type, void * buffer, size_t * size, T value )
{
    check_info_size( buffer, size, sizeof( T ) );
    if( type )
        *type = data_type;
    if( buffer )
        std::memcpy( buffer, &value, sizeof( T ) );
    *size = sizeof( T );
}

inline void info_bool( INFO_DATATYPE * type, void * buffer, size_t * size, bool value )
{
    info_value< uint8_t >( INFO_DATATYPE_BOOL8, type, buffer, size, value ? 1 : 0 );
}

inline void info_sizet( INFO_DATATYPE * type, void * buffer, size_t * size, size_t value )
{
    info_value( INFO_DATATYPE_SIZET, type, buffer, size, value );
}

inline void info_u64( INFO_DATATYPE * type, void * buffer, size_t * size, uint64_t value )
{
    info_value( INFO_DATATYPE_UINT64, type, buffer, size, value );
}

inline void info_ptr( INFO_DATATYPE * type, void * buffer, size_t * size, void * value )
{
    info_value( INFO_DATATYPE_PTR, type, buffer, size, value );
}

[[noreturn]] inline void unknown_info( int32_t cmd )
{
    throw gentl_error( GC_ERR_INVALID_ID, "unsupported info command " + std::to_string( cmd ) );
}

}  // namespace rs2_gentl
