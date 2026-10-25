// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

#include "node-map.h"
#include "gentl-common.h"

#include <algorithm>
#include <cstdio>
#include <cstring>

namespace rs2_gentl {

namespace {

const size_t INT_LENGTH = 8;
const size_t STRING_LENGTH = 64;

std::string escape( const std::string & text )
{
    std::string out;
    for( char c : text )
    {
        switch( c )
        {
        case '&': out += "&amp;"; break;
        case '<': out += "&lt;"; break;
        case '>': out += "&gt;"; break;
        case '"': out += "&quot;"; break;
        default: out += c;
        }
    }
    return out;
}

// Consumers cache parsed XML by VersionGuid, so it must change whenever the generated content does
std::string content_guid( const std::string & content )
{
    uint64_t h1 = 14695981039346656037ULL, h2 = 1099511628211ULL * 31;
    for( unsigned char c : content )
    {
        h1 = ( h1 ^ c ) * 1099511628211ULL;
        h2 = ( h2 ^ c ) * 1099511628211ULL + 0x9E3779B97F4A7C15ULL;
    }
    char guid[40];
    std::snprintf( guid, sizeof( guid ), "%08X-%04X-%04X-%04X-%012llX", unsigned( h1 >> 32 ),
                   unsigned( ( h1 >> 16 ) & 0xFFFF ), unsigned( h1 & 0xFFFF ), unsigned( h2 >> 48 ),
                   (unsigned long long)( h2 & 0xFFFFFFFFFFFFULL ) );
    return guid;
}

}  // namespace

node_map::node_map( std::string port_name, std::string model_name, std::string product_guid )
    : _port_name( std::move( port_name ) )
    , _model_name( std::move( model_name ) )
    , _product_guid( std::move( product_guid ) )
{
}

uint64_t node_map::add_register( size_t length, const options & opt, std::function< void( int, uint8_t * ) > read,
                                 std::function< void( int, const uint8_t * ) > write )
{
    uint64_t address = _regs.size() * REGISTER_STRIDE;
    _regs.push_back( { address, length, opt.count, std::move( read ), std::move( write ) } );
    return address;
}

void node_map::register_node( const char * type, const std::string & name, uint64_t address, size_t length,
                              bool writable, const options & opt, const char * tail )
{
    _nodes << "  <" << type << " Name=\"" << name << "\">\n"
           << "    <Visibility>Invisible</Visibility>\n"
           << "    <Address>0x" << std::hex << address << std::dec << "</Address>\n";
    if( ! opt.selector.empty() )
        _nodes << "    <pIndex Offset=\"" << length << "\">" << opt.selector << "</pIndex>\n";
    _nodes << "    <Length>" << length << "</Length>\n"
           << "    <AccessMode>" << ( writable ? "RW" : "RO" ) << "</AccessMode>\n"
           << "    <pPort>" << _port_name << "</pPort>\n"
           << "    <Cachable>NoCache</Cachable>\n"
           << tail << "  </" << type << ">\n";
}

void node_map::open_feature( const char * type, const std::string & name, const options & opt )
{
    _nodes << "  <" << type << " Name=\"" << name << "\" NameSpace=\"" << ( opt.standard ? "Standard" : "Custom" )
           << "\">\n";
    if( ! opt.tooltip.empty() )
        _nodes << "    <ToolTip>" << escape( opt.tooltip ) << "</ToolTip>\n";
    _nodes << "    <Visibility>Beginner</Visibility>\n";
    if( ! opt.selector.empty() )
        _selected[opt.selector].push_back( name );
}

std::string node_map::add_int_register( const std::string & name, int_getter get, int_setter set, const options & opt )
{
    bool writable = static_cast< bool >( set );
    std::function< void( int, const uint8_t * ) > write;
    if( writable )
        write = [set]( int index, const uint8_t * in ) {
            int64_t value;
            std::memcpy( &value, in, INT_LENGTH );
            set( index, value );
        };
    uint64_t address = add_register(
        INT_LENGTH, opt,
        [get]( int index, uint8_t * out ) {
            int64_t value = get( index );
            std::memcpy( out, &value, INT_LENGTH );
        },
        write );
    std::string reg_name = name + "Reg";
    register_node( "IntReg", reg_name, address, INT_LENGTH, writable, opt,
                   "    <Sign>Signed</Sign>\n    <Endianess>LittleEndian</Endianess>\n" );
    return reg_name;
}

std::string node_map::add_float_register( const std::string & name, float_getter get, float_setter set,
                                          const options & opt )
{
    bool writable = static_cast< bool >( set );
    std::function< void( int, const uint8_t * ) > write;
    if( writable )
        write = [set]( int index, const uint8_t * in ) {
            double value;
            std::memcpy( &value, in, sizeof( value ) );
            set( index, value );
        };
    uint64_t address = add_register(
        sizeof( double ), opt,
        [get]( int index, uint8_t * out ) {
            double value = get( index );
            std::memcpy( out, &value, sizeof( value ) );
        },
        write );
    std::string reg_name = name + "Reg";
    register_node( "FloatReg", reg_name, address, sizeof( double ), writable, opt,
                   "    <Endianess>LittleEndian</Endianess>\n" );
    return reg_name;
}

void node_map::add_category( const std::string & name, const std::vector< std::string > & features, bool standard )
{
    options opt;
    opt.standard = standard;
    open_feature( "Category", name, opt );
    for( auto & feature : features )
        _nodes << "    <pFeature>" << feature << "</pFeature>\n";
    _nodes << "  </Category>\n";
}

void node_map::add_string( const std::string & name, std::function< std::string() > get, const options & opt )
{
    uint64_t address = add_register( STRING_LENGTH, opt,
                                     [get]( int, uint8_t * out ) {
                                         std::string value = get();
                                         std::memset( out, 0, STRING_LENGTH );
                                         std::memcpy( out, value.c_str(), std::min( value.size(), STRING_LENGTH - 1 ) );
                                     },
                                     nullptr );
    open_feature( "StringReg", name, opt );
    _nodes << "    <Address>0x" << std::hex << address << std::dec << "</Address>\n"
           << "    <Length>" << STRING_LENGTH << "</Length>\n"
           << "    <AccessMode>RO</AccessMode>\n"
           << "    <pPort>" << _port_name << "</pPort>\n"
           << "    <Cachable>NoCache</Cachable>\n"
           << "  </StringReg>\n";
}

void node_map::add_integer( const std::string & name, int_getter get, int_setter set, const options & opt,
                            int_getter min, int_getter max )
{
    std::string value = add_int_register( name, get, set, opt );
    std::string min_reg = min ? add_int_register( name + "Min", min, nullptr, opt ) : "";
    std::string max_reg = max ? add_int_register( name + "Max", max, nullptr, opt ) : "";
    open_feature( "Integer", name, opt );
    _nodes << "    <pValue>" << value << "</pValue>\n";
    if( min )
        _nodes << "    <pMin>" << min_reg << "</pMin>\n";
    if( max )
        _nodes << "    <pMax>" << max_reg << "</pMax>\n";
    if( ! opt.unit.empty() )
        _nodes << "    <Unit>" << opt.unit << "</Unit>\n";
    _nodes << "  </Integer>\n";
}

void node_map::add_float( const std::string & name, float_getter get, float_setter set, const options & opt,
                          float_getter min, float_getter max )
{
    std::string value = add_float_register( name, get, set, opt );
    std::string min_reg = min ? add_float_register( name + "Min", min, nullptr, opt ) : "";
    std::string max_reg = max ? add_float_register( name + "Max", max, nullptr, opt ) : "";
    open_feature( "Float", name, opt );
    _nodes << "    <pValue>" << value << "</pValue>\n";
    if( min )
        _nodes << "    <pMin>" << min_reg << "</pMin>\n";
    if( max )
        _nodes << "    <pMax>" << max_reg << "</pMax>\n";
    if( ! opt.unit.empty() )
        _nodes << "    <Unit>" << opt.unit << "</Unit>\n";
    _nodes << "  </Float>\n";
}

void node_map::add_boolean( const std::string & name, int_getter get, int_setter set, const options & opt )
{
    std::string value = add_int_register( name, get, set, opt );
    open_feature( "Boolean", name, opt );
    _nodes << "    <pValue>" << value << "</pValue>\n"
           << "    <OnValue>1</OnValue>\n"
           << "    <OffValue>0</OffValue>\n"
           << "  </Boolean>\n";
}

void node_map::add_enumeration( const std::string & name, const enum_entries & entries, int_getter get,
                                int_setter set, const options & opt )
{
    std::string value = add_int_register( name, get, set, opt );
    open_feature( "Enumeration", name, opt );
    for( auto & entry : entries )
        _nodes << "    <EnumEntry Name=\"" << entry.first << "\" NameSpace=\"" << ( opt.standard ? "Standard" : "Custom" )
               << "\">\n      <Value>" << entry.second << "</Value>\n    </EnumEntry>\n";
    _nodes << "    <pValue>" << value << "</pValue>\n"
           << "    <!--pSelected:" << name << "-->\n"
           << "  </Enumeration>\n";
}

void node_map::add_command( const std::string & name, std::function< void() > execute, const options & opt )
{
    // Reads return 0 so GenApi's IsDone() sees the command as complete once the write returns
    std::string value = add_int_register(
        name, []( int ) { return int64_t( 0 ); },
        [execute]( int, int64_t ) { execute(); }, opt );
    open_feature( "Command", name, opt );
    _nodes << "    <pValue>" << value << "</pValue>\n"
           << "    <CommandValue>1</CommandValue>\n"
           << "  </Command>\n";
}

void node_map::finalize()
{
    std::string body = _nodes.str();
    for( auto & selector : _selected )
    {
        std::string placeholder = "    <!--pSelected:" + selector.first + "-->\n";
        std::string selected;
        for( auto & feature : selector.second )
            selected += "    <pSelected>" + feature + "</pSelected>\n";
        auto pos = body.find( placeholder );
        if( pos != std::string::npos )
            body.replace( pos, placeholder.size(), selected );
    }
    std::string::size_type pos;
    while( ( pos = body.find( "    <!--pSelected:" ) ) != std::string::npos )
        body.erase( pos, body.find( "\n", pos ) - pos + 1 );
    body += "  <Port Name=\"" + _port_name + "\" NameSpace=\"Standard\"/>\n";

    std::ostringstream xml;
    xml << "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
        << "<RegisterDescription ModelName=\"" << _model_name << "\" VendorName=\"" << VENDOR_NAME << "\""
        << " ToolTip=\"RealSense GenTL producer\" StandardNameSpace=\"None\""
        << " SchemaMajorVersion=\"1\" SchemaMinorVersion=\"1\" SchemaSubMinorVersion=\"0\""
        << " MajorVersion=\"1\" MinorVersion=\"0\" SubMinorVersion=\"0\""
        << " ProductGuid=\"" << _product_guid << "\" VersionGuid=\"" << content_guid( _model_name + body ) << "\""
        << " xmlns=\"http://www.genicam.org/GenApi/Version_1_1\""
        << " xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\""
        << " xsi:schemaLocation=\"http://www.genicam.org/GenApi/Version_1_1"
           " http://www.genicam.org/GenApi/GenApiSchema_Version_1_1.xsd\">\n"
        << body << "</RegisterDescription>\n";
    _xml = xml.str();
}

std::string node_map::url() const
{
    char url[256];
    std::snprintf( url, sizeof( url ), "Local:%s;%llx;%llx", xml_file_name().c_str(),
                   (unsigned long long)XML_ADDRESS, (unsigned long long)_xml.size() );
    return url;
}

const node_map::reg & node_map::find( uint64_t address, int & index, size_t & offset ) const
{
    size_t slot = size_t( address / REGISTER_STRIDE );
    if( slot < _regs.size() )
    {
        const reg & r = _regs[slot];
        uint64_t relative = address - r.address;
        if( relative < r.length * r.count )
        {
            index = int( relative / r.length );
            offset = size_t( relative % r.length );
            return r;
        }
    }
    throw gentl_error( GC_ERR_INVALID_ADDRESS, "no register at address " + std::to_string( address ) );
}

void node_map::read( uint64_t address, void * buffer, size_t size ) const
{
    if( address >= XML_ADDRESS )
    {
        uint64_t offset = address - XML_ADDRESS;
        if( offset + size > _xml.size() )
            throw gentl_error( GC_ERR_INVALID_ADDRESS, "read past the end of the XML" );
        std::memcpy( buffer, _xml.data() + offset, size );
        return;
    }
    int index;
    size_t offset;
    const reg & r = find( address, index, offset );
    if( offset + size > r.length )
        throw gentl_error( GC_ERR_INVALID_PARAMETER, "read crosses a register boundary" );
    uint8_t value[STRING_LENGTH];
    r.read( index, value );
    std::memcpy( buffer, value + offset, size );
}

void node_map::write( uint64_t address, const void * buffer, size_t size )
{
    int index;
    size_t offset;
    const reg & r = find( address, index, offset );
    if( ! r.write )
        throw gentl_error( GC_ERR_ACCESS_DENIED, "register is read-only" );
    if( offset != 0 || size != r.length )
        throw gentl_error( GC_ERR_INVALID_PARAMETER, "partial register writes are not supported" );
    r.write( index, static_cast< const uint8_t * >( buffer ) );
}

}  // namespace rs2_gentl
